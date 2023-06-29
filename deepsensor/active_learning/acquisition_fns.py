import copy

import numpy as np

from scipy.stats import norm

from deepsensor.model.model import ProbabilisticModel
from deepsensor.data.task import Task


class AcquisitionFunction:
    """
    Parent class for acquisition functions.
    """

    def __init__(self, model: ProbabilisticModel):
        """
        Args:
            model (ProbabilisticModel):
            context_set_idx (int): Index of context set to add new observations to when computing
                the acquisition function.
        """
        self.model = model

    def __call__(self, task: Task):
        """
        Args:
            task (Task): Task object containing context and target sets.

        Returns:
            np.ndarray: Acquisition function value/s. Shape ().
        """
        raise NotImplementedError


class AcquisitionFunctionParallel(AcquisitionFunction):
    """
    Parent class for acquisition functions that are computed across all search points in parallel.
    """

    def __call__(self, task: Task, X_s: np.ndarray):
        """
        Args:
            task (Task): Task object containing context and target sets.
            X_s (np.ndarray): Search points. Shape (2, N_search).

        Returns:
            np.ndarray: Acquisition function value/s. Shape (N_search,).
        """
        raise NotImplementedError


class MeanStddev(AcquisitionFunction):
    """Mean of the marginal variances."""

    def __call__(self, task, target_set_idx=0):
        return np.mean(self.model.stddev(task)[target_set_idx])


class MeanVariance(AcquisitionFunction):
    """Mean of the marginal variances."""

    def __call__(self, task, target_set_idx=0):
        return np.mean(self.model.variance(task)[target_set_idx])


class pNormStddev(AcquisitionFunction):
    """p-norm of the vector of marginal standard deviations."""

    def __init__(self, *args, p=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.p = p

    def __call__(self, task, target_set_idx=0):
        return np.linalg.norm(
            self.model.stddev(task)[target_set_idx].ravel(), ord=self.p
        )


class MeanMarginalEntropy(AcquisitionFunction):
    """Mean of the entropies of the marginal predictive distributions."""

    def __call__(self, task):
        marginal_entropy = self.model.mean_marginal_entropy(task)
        return marginal_entropy


class JointEntropy(AcquisitionFunction):
    """Joint entropy of the predictive distribution."""

    def __call__(self, task):
        return self.model.joint_entropy(task)


class Random(AcquisitionFunctionParallel):
    """Random acquisition function."""

    def __init__(self, seed: int = 42):
        self.rng = np.random.default_rng(seed)

    def __call__(self, task, X_s):
        return self.rng.random(X_s.shape[1])


class ContextDist(AcquisitionFunctionParallel):
    """Distance to closest context point."""

    def __init__(self, context_set_idx):
        self.context_set_idx = context_set_idx

    def __call__(self, task, X_s):
        X_c = task["X_c"][self.context_set_idx]

        if X_c.size == 0:
            # No sensors placed yet, so arbitrarily choose first query point by setting its
            #    acquisition fn to non-zero and all others to zero
            dist_to_closest_sensor = np.zeros(X_s.shape[-1])
            dist_to_closest_sensor[0] = 1
        else:
            # Use broadcasting to get matrix of distances from each possible
            #   new sensor location to each existing sensor location
            dists_all = np.linalg.norm(
                X_s[..., np.newaxis] - X_c[..., np.newaxis, :], axis=0
            )  # Shape (n_possible_locs, n_context + n_placed_sensors)

            # Compute distance to nearest sensor
            dist_to_closest_sensor = dists_all.min(axis=1)
        return dist_to_closest_sensor


class Stddev(AcquisitionFunctionParallel):
    """Random acquisition function."""

    def __call__(self, task, X_s, target_set_idx=0):
        # Set the target points to the search points
        task = copy.deepcopy(task)
        task["X_t"] = X_s

        return self.model.stddev(task)[target_set_idx]


class ExpectedImprovement(AcquisitionFunctionParallel):
    """Expected improvement acquisition function."""

    def __init__(self, model: ProbabilisticModel, context_set_idx: int = 0):
        """
        Args:
            model (ProbabilisticModel):
            context_set_idx (int): Index of context set to add new observations to when computing
                the acquisition function.
        """
        super().__init__(model)
        self.context_set_idx = context_set_idx

    def __call__(self, task: Task, X_s: np.ndarray, target_set_idx: int = 0):
        """
        Args:
            task (Task): Task object containing context and target sets.
            X_s (np.ndarray): Search points. Shape (2, N_search).
            target_set_idx (int): Index of target set to compute acquisition function for.

        Returns:
            np.ndarray: Acquisition function value/s. Shape (N_search,).
        """
        # Set the target points to the search points
        task = copy.deepcopy(task)
        task["X_t"] = X_s

        # Compute the predictive mean and variance of the target set
        mean = self.model.mean(task)[target_set_idx]

        # Compute the best target value seen so far
        best_target_value = task["Y_c"][self.context_set_idx].max()

        # Compute the standard deviation of the context set
        stddev = self.model.stddev(task)[self.context_set_idx]

        # Compute the expected improvement
        Z = (mean - best_target_value) / stddev
        ei = stddev * (mean - best_target_value) * norm.cdf(Z) + stddev * norm.pdf(Z)

        return ei