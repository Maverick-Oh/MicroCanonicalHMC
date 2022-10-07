### MicroCanoncial Hamiltonian Monte Carlo (MCHMC)

MCHMC is is a constant energy sampler from an arbitrary distribution with an available gradient of the probability density. Unlike the canonical HMC it does not require the equilibration between the different energy levels. Instead it tunes the Hamiltonian function such that the marginal over momentum variables over a single energy surface gives the desired target distribution. Consequently it is severalfold more efficient in terms of the required gradient calls for the same posterior quality.

You can check out the accompanying [paper] and the [tutorial](tutorial.ipynb).

If you have any questions do not hesitate to contact me at jakob_robnik@berkeley.edu
