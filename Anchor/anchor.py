import logging

from Anchor.visualizer import Visualizer

logging.basicConfig(level=logging.INFO)
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional, Protocol, Tuple, Union

import numpy as np
from ConfigSpace import ConfigurationSpace
from ConfigSpace.hyperparameters import UniformIntegerHyperparameter
from skimage.segmentation import quickshift
from smac.facade.smac_bb_facade import SMAC4BB
from smac.scenario.scenario import Scenario

from Anchor.bandit import KL_LUCB
from Anchor.candidate import AnchorCandidate
from Anchor.sampler import Sampler, Tasktype

from .visualizer import Visualizer


@dataclass()
class Anchor:
    """
    Approach to explain predictions of a blackbox model using anchors.
    It returns the explaination with a precision and coverage score.

    More details can be found in the following paper:
    https://homes.cs.washington.edu/~marcotcr/aaai18.pdf
    """

    tasktype: Tasktype
    sampler: Sampler = field(init=False)
    visualizer: Visualizer = field(init=False)
    verbose: bool = False
    coverage_data: np.array = field(init=False)

    def __post_init__(self):
        logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.DEBUG)

    def explain_instance(
        self,
        input: any,
        predict_fn: Callable[[any], np.array],
        method: str = "greedy",
        task_specific: dict = None,
        method_specific: dict = None,
        num_coverage_samples: int = 10000,
        epsilon: float = 0.1,
        delta: float = 0.1,
        batch_size: int = 16,
        verbose=False,
        seed=69,
    ):
        """
        Main entrance point to explain an instance.

        Args:
            input (Any): The instance to explain - can be an image, data row or text.
            predict_fn (Callable): A function that returns the class prediction for a sample (can be wrapped with provided wrapper functions).
            method (String): Defines the optimization function. Can be (``greedy``), (``beam``) or (``smac``).
            dataset (np.array): The dataset for permutation. Could be images for image task or tabular data for tabular task.
            task_specific (dict): Task specific arguments. For tabular this includes the (``column_names``) and (``dataset``) argument. For images it includes (``dataset``).
            method_specific (dict): Optimization method specific arguments. For Beam Search this includes (``beam_size``) and (``desired_confidence``). 
                For greedy this includes (``desired_confidence``). For Smac this includes (``run_time``) in seconds and (``optim``). 
                Optim is a function with the signature AnchorCandiate -> float that will be minimized.
            num_coverage_samples (int): Number of coverage samples
            desired_confidence (float): desired precision confidence for the anchor.
            epsilon (float)
            batch_size (int)
            verbose (bool)

        Returns:
            exp (AnchorCandidate): The explanation of the original instance.

        """
        self.seed = seed
        np.random.seed(seed)

        # in case args are empty
        if task_specific is None:
            task_specific = {}

        if method_specific is None:
            method_specific = {}

        self.kl_lucb = KL_LUCB(
            eps=epsilon, delta=delta, batch_size=batch_size, verbose=verbose
        )
        self.sampler = Sampler.create(self.tasktype, input, predict_fn, task_specific)

        self.batch_size = batch_size
        self.delta = delta
        logging.info(" Start Sampling")
        _, self.coverage_data = self.sampler.sample(
            AnchorCandidate(feature_mask=[]), num_coverage_samples, False
        )
        exp = AnchorCandidate(feature_mask=[])
        if method == "greedy":
            logging.info(" Start Greedy Search")
            exp = self.__greedy_anchor(**method_specific)
        elif method == "beam":
            logging.info(" Start Beam Search")
            exp = self.__beam_anchor(**method_specific)
        elif method == "smac":
            logging.info(" Start SMAC Search")

            exp = self.__smac_anchor(**method_specific)

        return exp

    def visualize(self, anchor: AnchorCandidate, instance: np.ndarray):
        """
        Visualized the instance given the anchor.

        Args:
            anchor (AnchorCandidate): Anchor (usually result of explain_instance)
            instance (np.ndarray): Instance that shall be explained by the anchor.
        """

        # as text sampler has no self.features
        try:
            features = self.sampler.features
        except AttributeError:
            features = None

        return Visualizer.create(self.tasktype).visualize(anchor, instance, features)

    def generate_candidates(
        self, prev_anchors: list[AnchorCandidate], coverage_min: float
    ) -> list[AnchorCandidate]:
        """
        Generates new anchor candidates by adding a new unseen feature
        to each previous anchor feature mask.

        Args:
            prev_anchors (list[AnchorCandidate]): previous anchors.
            coverage_min (float): min_coverage an anchor must have.

        Returns:
            list[AnchorCandidate]: new anchor candidates
        """
        new_candidates: list[AnchorCandidate] = []
        # iterate over possible features or predicates
        for feature in range(self.sampler.num_features):
            # check if we have no prev anchors and create a complete new set
            if len(prev_anchors) == 0:
                nc = AnchorCandidate(feature_mask=[feature])
                new_candidates.append(nc)

            for anchor in prev_anchors:
                # check if feature already in the feature_mask of the anchor
                if feature in anchor.feature_mask:
                    continue

                # append new feature to candidate
                tmp = anchor.feature_mask.copy()
                tmp.append(feature)

                nc = AnchorCandidate(feature_mask=tmp)
                nc.coverage = self.__calculate_coverage(nc)
                if nc.coverage >= coverage_min:
                    new_candidates.append(nc)

        return new_candidates

    def __calculate_coverage(self, anchor: AnchorCandidate) -> float:
        """
        Calculates the coverage for an given anchor.

        Args:
            anchor (AnchorCandidate): Anchor for which the coverage shall be calculated.

        Returns:
            float: Coverage
        """
        included_samples = 0
        for mask in self.coverage_data:  # replace with numpy only
            # check if mask positive samples are included in the feature_mask of the anchor
            if np.all(np.isin(anchor.feature_mask, np.where(mask == 1)), axis=0):
                included_samples += 1

        return included_samples / self.coverage_data.shape[0]

    def __check_valid_candidate(
        self,
        candidate: AnchorCandidate,
        beam_size: int,
        sample_count: int,
        dconf: float,
        delta: float = 0.1,
        eps_stop: float = 0.05,
    ) -> bool:
        """
        Checks if an candidate fullfills precision boundary constraints
        """
        prec = candidate.precision
        beta = np.log(1.0 / (delta / (1 + (beam_size - 1) * self.sampler.num_features)))

        lb = KL_LUCB.dlow_bernoulli(prec, beta / max(candidate.n_samples, 1))
        ub = KL_LUCB.dup_bernoulli(prec, beta / max(candidate.n_samples, 1))

        while (prec >= dconf and lb < dconf - eps_stop) or (
            prec < dconf and ub >= dconf + eps_stop
        ):
            nc, _ = self.sampler.sample(candidate, sample_count)
            prec = nc.precision
            lb = KL_LUCB.dlow_bernoulli(prec, beta / nc.n_samples)

            ub = KL_LUCB.dup_bernoulli(prec, beta / nc.n_samples)

        return prec >= dconf and lb > dconf - eps_stop

    def __greedy_anchor(
        self, desired_confidence: float = 1, min_coverage: float = 0.2,
    ):
        """
        Greedy Approach to calculate the shortest anchor, which fullfills the precision constraint EQ3.

        Args:
            desired_confidence (float): desired approximated precision
            min_coverage (float): min coverage an anchor needs to be accepted

        Returns:
            AnchorCandidate: best found anchor
        """
        candidates = self.generate_candidates([], min_coverage)
        anchor = self.kl_lucb.get_best_candidates(candidates, self.sampler, 1)[0]

        while not self.__check_valid_candidate(
            anchor, 1, self.batch_size, desired_confidence, self.delta
        ):
            candidates = self.generate_candidates([anchor], min_coverage)

            # no more candiates return the best one so far
            if len(candidates) == 0:
                break

            anchor = self.kl_lucb.get_best_candidates(candidates, self.sampler, 1)[0]

        return anchor

    def __beam_anchor(
        self, desired_confidence: float, beam_size: int,
    ) -> AnchorCandidate:
        """
        Beam search algorithm to find anchor.

        Args:
            desired_confidence (float): Desired confidence before stopping.
            beam_size (int): Beam size

        Returns:
            AnchorCandidate: best found anchor
        """
        max_anchor_size = self.sampler.num_features
        current_anchor_size = 1
        best_of_size = {0: []}  # A0
        best_candidate = AnchorCandidate([])  # A*

        while current_anchor_size < max_anchor_size:
            # Generate candidates
            candidates = self.generate_candidates(
                best_of_size[current_anchor_size - 1], best_candidate.coverage,
            )

            # no more candiates return best one so far
            if len(candidates) == 0:
                break

            best_candidates = self.kl_lucb.get_best_candidates(
                candidates, self.sampler, min(beam_size, len(candidates))
            )

            best_of_size[current_anchor_size] = best_candidates

            # update best candiate when its valid and has a better
            # coverage.
            for c in best_candidates:
                if (
                    self.__check_valid_candidate(
                        c,
                        beam_size=beam_size,
                        sample_count=self.batch_size,
                        dconf=desired_confidence,
                        delta=self.delta,
                    )
                    and c.coverage > best_candidate.coverage
                ):
                    best_candidate = c

            current_anchor_size += 1

        return best_candidate

    def __smac_anchor(
        self, run_time: int, optim: Callable[[AnchorCandidate], float] = None
    ) -> AnchorCandidate:
        """
        Utilites smac to find an anchor.

        Args:
            run_time (int): Amount of wallclock time to run the optimization for.
        Returns:
            AnchorCandidate: best found anchor
        """

        self.smac_optim_func = optim if optim is not None else None

        # create config space
        configspace = ConfigurationSpace()

        # mask the possible features
        for i in range(self.sampler.num_features):
            configspace.add_hyperparameter(UniformIntegerHyperparameter(str(i), 0, 1))

        # create Szenario
        scenario = Scenario(
            {
                "run_obj": "quality",
                "wallclock_limit": run_time,
                "cs": configspace,
                "deterministic": "true",  # each config gets evaluated once, other option would be to track candidates and average precision / coverage
            }
        )

        # create optimizer
        smac = SMAC4BB(
            scenario=scenario,
            tae_runner=self.smac_optimize,
            rng=np.random.RandomState(self.seed),
        )
        best_mask = smac.optimize()

        feature_mask = [int(f_idx) for f_idx, mv in best_mask.items() if mv]
        stats = smac.runhistory.data[
            next(reversed(smac.runhistory.data))
        ].additional_info

        return AnchorCandidate(
            feature_mask=feature_mask,
            precision=stats["precision"],
            coverage=stats["coverage"],
        )

    def smac_optimize(self, config):
        """
        Main bayesian optimization loop for smac.

        Args:
            config (Configspace): Current feature configuration to be evalauted.
        """
        feature_mask = [int(f_idx) for f_idx, mv in config.items() if mv]

        # create candidate from config which is the feature mask to evaluate
        candidate = AnchorCandidate(feature_mask)

        # calculate expected precision
        candidate, _ = self.sampler.sample(candidate, self.batch_size)
        candidate.coverage = self.__calculate_coverage(candidate)

        info = {"precision": candidate.precision, "coverage": candidate.coverage}

        if self.smac_optim_func is not None:
            return self.smac_optim_func(candidate), info
        else:
            return (
                (
                    (1 - candidate.precision)
                    + (len(candidate.feature_mask) / self.sampler.num_features)
                )
                / 2,
                info,
            )

