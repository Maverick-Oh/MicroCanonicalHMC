import matplotlib.pyplot as plt
import numpy as np
import jax
import jax.numpy as jnp
import pandas as pd


from .sampler import find_crossing
from .sampler import my_while



class vmap_target:
    """A wrapper target class, where jax.vmap has been applied to the functions of a given target"""

    def __init__(self, target):
        """target: a given target to vmap"""

        # obligatory attributes
        self.nlogp = jax.vmap(target.nlogp)
        self.grad_nlogp = jax.vmap(target.grad_nlogp)
        self.d = target.d


        # optional attributes

        if hasattr(target, 'transform'):
            self.transform = jax.vmap(jax.vmap(target.transform))
        else:
            self.transform = lambda x: x #if not given, set it to the identity

        if hasattr(target, 'prior_draw'):
            self.prior_draw = jax.vmap(target.prior_draw)

        if hasattr(target, 'variance'):
            self.variance = target.variance


class pmap_target:
    """A wrapper target class, where jax.pmap has been applied to the functions of a given target"""

    def __init__(self, target):
        """target: a given target to pmap"""

        # obligatory attributes
        self.nlogp = jax.pmap(target.nlogp)
        self.grad_nlogp = jax.pmap(target.grad_nlogp)
        self.d = target.d

        # optional attributes

        if hasattr(target, 'transform'):
            self.transform = jax.pmap(jax.vmap(target.transform))
        else:
            self.transform = lambda x: x  # if not given, set it to the identity

        if hasattr(target, 'prior_draw'):
            self.prior_draw = jax.pmap(target.prior_draw)

        if hasattr(target, 'variance'):
            self.variance = target.variance



class Sampler:
    """Ensamble MCHMC (q = 0 Hamiltonian) sampler"""

    def __init__(self, Target, pmap = False):
        """Args:
                Target: the target distribution class.
                pmap: if True, jax.pmap will be applied to the Target functions.
                      In this case, the number of available devices (as returned by jax.local_device_count())
                      should be equal or larger than the number of ensamble chains that we are running.
                      if False, jax.vmap will be applied to the Target functions. The operation will be run on a single device.
        """

        if pmap:
            self.Target = pmap_target(Target)
        else:
            self.Target = vmap_target(Target)

        self.grad_evals_per_step = 1.0 #per chain



    def random_unit_vector(self, key, num_chains):
        """Generates a random (isotropic) unit vector."""
        key, subkey = jax.random.split(key)
        u = jax.random.normal(subkey, shape = (num_chains, self.Target.d), dtype = 'float64')
        normed_u = u / jnp.sqrt(jnp.sum(jnp.square(u), axis = 1))[:, None]
        return normed_u, key


    def partially_refresh_momentum(self, u, key, nu):
        """Adds a small noise to u and normalizes."""
        key, subkey = jax.random.split(key)
        noise = nu * jax.random.normal(subkey, shape= u.shape, dtype=u.dtype)

        return (u + noise) / jnp.sqrt(jnp.sum(jnp.square(u + noise), axis = 1))[:, None], key


    def update_momentum(self, eps, g, u):
        """The momentum updating map of the esh dynamics (see https://arxiv.org/pdf/2111.02434.pdf)"""
        g_norm = jnp.sqrt(jnp.sum(jnp.square(g), axis = 1)).T
        e = - g / g_norm[:, None]
        ue = jnp.sum(u*e, axis = 1)
        sh = jnp.sinh(eps * g_norm / (self.Target.d - 1))
        ch = jnp.cosh(eps * g_norm / (self.Target.d - 1))
        th = jnp.tanh(eps * g_norm / (self.Target.d-1))
        delta_r = jnp.log(ch) + jnp.log1p(ue * th)

        return (u + e * (sh + ue * (ch - 1))[:, None]) / (ch + ue * sh)[:, None], delta_r


    def hamiltonian_dynamics(self, x, u, g, key, eps, sigma):
        """leapfrog"""

        z = x / sigma # go to the latent space

        # half step in momentum
        uu, delta_r1 = self.update_momentum(eps * 0.5, g * sigma, u)

        # full step in x
        zz = z + eps * uu
        xx = sigma * zz # go back to the configuration space
        l, gg = self.Target.grad_nlogp(xx)

        # half step in momentum
        uu, delta_r2 = self.update_momentum(eps * 0.5, gg * sigma, uu)
        kinetic_change = (delta_r1 + delta_r2) * (self.Target.d-1)

        return xx, uu, l, gg, kinetic_change, key


    def dynamics(self, x, u, g, key, L, eps, sigma):
        """One step of the generalized dynamics."""

        # Hamiltonian step
        xx, uu, ll, gg, kinetic_change, key = self.hamiltonian_dynamics(x, u, g, key, eps, sigma)

        # bounce
        nu = jnp.sqrt((jnp.exp(2 * eps / L) - 1.0) / self.Target.d)
        uu, key = self.partially_refresh_momentum(uu, key, nu)

        return xx, uu, ll, gg, kinetic_change, key


    def full_b(self, X):

        X_sq = jnp.average(jnp.square(X), axis= 0)

        def step(F2, index):
            x_sq = X_sq[index, :]
            F2_new = (F2 * index + x_sq) / (index + 1)  # Update <f(x)> with a Kalman filter
            b = jnp.sqrt(jnp.average(jnp.square((F2_new - self.Target.variance) / self.Target.variance)))

            return F2_new, b

        return jax.lax.scan(step, jnp.zeros(self.Target.d), xs=jnp.arange(len(X_sq)))[1]


    def virial_loss(self, x, g):
        """loss^2 = (1/d) sum_i (virial_i - 1)^2"""

        virials = jnp.average(x*g, axis=0) #should be all close to 1 if we have reached the typical set
        return jnp.sqrt(jnp.average(jnp.square(virials - 1.0)))


    def initialize(self, random_key, x_initial, num_chains):


        if random_key is None:
            key = jax.random.PRNGKey(0)
        else:
            key = random_key

        if isinstance(x_initial, str):
            if x_initial == 'prior':  # draw the initial x from the prior
                keys_all = jax.random.split(key, num_chains + 1)
                x = self.Target.prior_draw(keys_all[1:])
                key = keys_all[0]

            else:  # if not 'prior' the x_initial should specify the initial condition
                raise KeyError('x_initial = "' + x_initial + '" is not a valid argument. \nIf you want to draw initial condition from a prior use x_initial = "prior", otherwise specify the initial condition with an array')

        else:  # initial x is given
            x = jnp.copy(x_initial)


        l, g = self.Target.grad_nlogp(x)

        ### initial velocity ###
        virials = jnp.average(x * g, axis=0)
        loss = jnp.sqrt(jnp.average(jnp.square(virials - 1.0)))
        sgn = -2.0 * (virials < 1.0) + 1.0
        u = - g / jnp.sqrt(jnp.sum(jnp.square(g), axis = 1))[:, None] # initialize momentum in the direction of the gradient of log p
        u = u * sgn[None, :] #if the virial in that direction is smaller than 1, we flip the direction of the momentum in that direction

        # u, key = self.random_unit_vector(key, num_chains) #random velocity orientations

        return loss, x, u, l, g, key


    def burn_in(self, loss, x, u, l, g, key):
        """Initial stage of the burn-in. Here the goal is to get to the typical set as quickly as possible (as measured by the virial conditions)."""

        ### hyperparameters of the burn in (the value of those parameters typically will not have large impact on the performance)###

        # important parameters
        L = jnp.sqrt(self.Target.d) * 1     #currently not set automatically
        varE = 1e-4                         #we aim for Var[E] / d = 'varE'. This will determine the stepsize during the sampling.

        # less important parameters
        eps = jnp.sqrt(self.Target.d)       #this will be changed during the burn-in
        max_burn_in = 200                   #we will not take more steps
        max_fail = 6                        #if the reduction of epsilon does not improve the loss 'max_fail'-times in a row, we stop the initial stage of burn-in
        virial_target = 0.1                 #if the virial loss is lower, we stop the initial stage of burn-in
        increase, reduce = 2.0, 0.5         #if the loss never went up, we incease the epsilon by a factor of 'increase'. If the loss went up, we decrease the epsilon by a factor 'reduce'.
        num_energy_points = 5               #how many points will we use to determine Var[E] (epsilon)  dependence.


        def accept_reject_step(loss, x, u, l, g, loss_new, xx, uu, ll, gg):
            """if there are nans or the loss went up we don't want to update the state"""

            no_nans = jnp.all(jnp.isfinite(xx))
            tru = (loss_new < loss) * no_nans  # loss went down and there were no nans
            false = (1 - tru)
            Loss = loss_new * tru + loss * false
            X = jnp.nan_to_num(xx) * tru + x * false
            U = jnp.nan_to_num(uu) * tru + u * false
            L = jnp.nan_to_num(ll) * tru + l * false
            G = jnp.nan_to_num(gg) * tru + g * false
            return tru, Loss, X, U, L, G


        def energy_variance(eps):
            """detemrine Var[E]/d at given epsilon"""
            xx, uu, ll, gg, kinetic_change, kkey = self.dynamics(x, u, g, key, L, eps, sigma)  # update particles by one step
            energy_change = kinetic_change + ll - l
            return jnp.average(jnp.square(energy_change)) / self.Target.d



        def burn_in_step(state):
            """one step of the burn in"""

            steps, loss, fail_count, never_rejected, x, u, l, g, key, L, eps, sigma = state
            sigma = jnp.std(x, axis=0)  # diagonal conditioner

            xx, uu, ll, gg, kinetic_change, key = self.dynamics(x, u, g, key, L, eps, sigma)  # update particles by one step

            loss_new = self.virial_loss(xx, gg)

            #will we accept the step?
            accept, loss, x, u, l, g = accept_reject_step(loss, x, u, l, g, loss_new, xx, uu, ll, gg)

            Ls.append(loss)
            #X.append(x)
            never_rejected *= accept #True if no step has been rejected so far
            fail_count = (fail_count + 1) * (1-accept)

                            #reduce eps if rejected    #increase eps if never rejected        #keep the same
            eps = eps * ((1-accept) * reduce + accept * (never_rejected * increase + (1-never_rejected) * 1.0))
            epss.append(eps)

            #energy_change = kinetic_change + ll - l
            #eng.append(jnp.average(jnp.square(energy_change)) / self.Target.d)

            return steps + 1, loss, fail_count, never_rejected, x, u, l, g, key, L, eps, sigma

        Ls = []
        epss = []
        # X = []

        condition = lambda state: (state[1] > virial_target)*(state[0] < max_burn_in)*(state[2] < max_fail)  # true during the burn-in

        steps, loss, fail_count, never_rejected, x, u, l, g, key, L, eps, sigma = my_while(condition, burn_in_step, (0, loss, 0, True, x, u, l, g, key, L, eps, jnp.ones(self.Target.d)))
        # if you want to debug, replace jax.lax.while_loop with my_while

        #jax.lax.while_loop

        plt.plot(Ls, '.-', label = 'loss')
        plt.plot(epss, 'o', label = 'epsilon')
        plt.legend()
        plt.yscale('log')
        plt.xlabel('burn-in steps')
        plt.show()


        # X = np.array(X)
        # x_particles = X[:, :, 0].T
        # y_particles = X[:, :, self.Target.d].T
        #
        # for i in range(10):
        #     plt.plot(x_particles[i][0], y_particles[i][0], 'o', color ='tab:red')
        #     plt.plot(x_particles[i], y_particles[i], '.-')
        #
        # from sampling.benchmark_targets import get_contour_plot
        # from sampling.benchmark_targets import Rosenbrock
        # X, Y, Z = get_contour_plot(Rosenbrock(d = 2), np.linspace(-2, 4, 100), np.linspace(-2, 10, 100))
        # plt.contourf(X, Y, jnp.exp(-Z), cmap = 'cividis')
        # plt.show()

        ### determine the epsilon for sampling ###
        eps = eps * (1.0/reduce)**fail_count #the epsilon before the row of failures, we will take this as a baseline
        epsilon = eps * jnp.logspace(-1, 1, num_energy_points)  # some range around eps
        vars= jax.vmap(energy_variance)(epsilon)
        eps, success = eps_fit(epsilon, vars, varE)
        #print('stepsize for sampling: ', eps)

        ### let's do some checks and print warnings ###
        if steps == max_burn_in:
            print('Burn-in exceeded the predescribed number of iterations, loss = {0} but we aimed for 0.1'.format(loss))
        if not success:
            print('The determination of the step-size for sampling may be unreliable (the energy fluctuations may be more than a factor of 10 off the typical optimum).')


        return steps + num_energy_points, x, u, l, g, key, L, eps, sigma



    def sample(self, num_steps, num_chains, x_initial='prior', random_key= None, output = 'full'):
        """Args:
               num_steps: number of integration steps to take during the sampling. There will be some additional steps during the first stage of the burn-in (max 200).

               num_chains: number of independent chains, currently only tested for num_chains = 300 (ensamble regime).

               x_initial: initial condition for x, shape: (num_chains, d). Defaults to 'prior' in which case the initial condition is drawn from the prior distribution (self.Target.prior_draw).

               random_key: jax random seed, defaults to jax.random.PRNGKey(0).

               output: determines the output of the function. Currently supported:
                        'full': returns the samples, shape: (num_chains, num_samples, d)
                        'ess': the number gradient calls per chain needed to get the bias b2 bellow 0.1. In this case, self.Target.variance = <x_i^2>_true should be defined.

        """

        state = self.initialize(random_key, x_initial, num_chains) #initialize

        burnin_steps, x, u, l, g, key, L, eps, sigma = self.burn_in(*state) #burn-in
        print(eps, sigma)

        ### sampling ###

        def step(state, useless):
            x, u, g, key = state
            x, u, l, g, kinetic_change, key = self.dynamics(x, u, g, key, L, eps, sigma)  # update particles by one step
            return (x, u, g, key), x

        X = jax.lax.scan(step, init=(x, u, g, key), xs=None, length=num_steps)[1]
        X = jnp.swapaxes(X, 0, 1)
        ### remove additional burn in ###
        f = jnp.average(jnp.average(jnp.square(X), axis=0), axis=1)
        f_avg = jnp.average(f[-num_steps // 10])
        burnin2_steps = num_steps - find_crossing(-jnp.abs(f[::-1] - f_avg) / f_avg, -0.1)

        plt.plot(f, '.-')
        plt.plot([0, len(f) - 1], jnp.ones(2) * f_avg, color='black')
        plt.plot([0, len(f) -1], jnp.ones(2) * f_avg*1.1, color = 'black', alpha= 0.3)
        plt.plot([0, len(f) - 1], jnp.ones(2) * f_avg*0.9, color= 'black', alpha= 0.3)

        plt.xlabel('steps')
        plt.ylabel('f')
        plt.show()


        ### return results ###

        if output == 'ess': #we return the number of sampling steps (needed for b2 < 0.1) and the number of burn-in steps
            b2 = self.full_b(self.Target.transform(X[:, burnin2_steps:, :]))
            plt.plot(b2)
            plt.xlabel('# sampling steps')
            plt.ylabel('b2')
            plt.show()
            no_nans = 1-jnp.any(jnp.isnan(b2))
            cutoff_reached = b2[-1] < 0.1
            return (find_crossing(b2, 0.1), burnin_steps + burnin2_steps) if (no_nans and cutoff_reached) else (np.inf, burnin_steps + burnin2_steps)


        else:
            if output == 'full': #return the samples X
                return X[:, burnin2_steps:, :]

            else:
                raise ValueError('output = ' + output + 'is not a valid argument for the Sampler.sample')




def linfit(x, y):
    """Args: data vectors x and y

       Fits the linear model: y = k x + n
       Estimates the covariance matrix:
            Cov = [[sigma_n^2, sigma_{n k}]
                   [sigma_{n k}, sigma_k^2]]

       Returns: (optimal n, optimal k, Cov)
    """

    Sx = jnp.average(x)
    Sy = jnp.average(y)
    Sxx = jnp.average(jnp.square(x))
    Sxy = jnp.average(x * y)
    det = Sxx - Sx**2
    Cov = jnp.array([[Sxx, -Sx], [-Sx, 1.0]]) / det
    return (Sxx * Sy - Sx * Sxy) / det, (Sxy - Sx * Sy) / det, Cov


def eps_fit(eps, var, var0):
    """Args:
            eps: stepsize array
            var: Var[E]/d array
            var0: targeted value of Var[E]/d. For example 0.0005
       Returns:
           eps where var(eps) = var0
           success boolean: true if roughly 0.1 < var(eps) / var0 < 10
    """

    y_predict = jnp.log(var0)
    intercept, slope, Cov = linfit(jnp.log(eps), jnp.log(var))
    x_predict = (y_predict - intercept) / slope
    y_predict_err = jnp.sqrt(x_predict**2 * Cov[0, 0] + 2 * Cov[0, 1] * x_predict + Cov[1, 1])

    # plt.plot(eps, var, 'o', color='blue')
    # plt.plot(eps, jnp.exp(slope * jnp.log(eps) + intercept), '-', color = 'black', alpha = 0.5)
    # plt.plot(jnp.exp(x_predict), jnp.exp(y_predict), 'o', color ='tab:red')
    # plt.yscale('log')
    # plt.xscale('log')
    # plt.xlabel('epsilon')
    # plt.ylabel('Var[E] / d')
    # plt.show()

    return jnp.exp(x_predict), y_predict_err < 2.3 # = log(10)