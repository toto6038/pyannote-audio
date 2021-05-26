# MIT License
#
# Copyright (c) 2020-2021 CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Segmentation pipelines"""

import math
from itertools import combinations

import networkx as nx
import numpy as np

from pyannote.audio import Inference, Model, Pipeline
from pyannote.audio.core.io import AudioFile
from pyannote.audio.pipelines.utils import PipelineModel, get_devices, get_model
from pyannote.audio.utils.activations import split_activations, warmup_activations
from pyannote.audio.utils.permutation import permutate
from pyannote.audio.utils.signal import Binarize
from pyannote.core import Annotation, SlidingWindow, SlidingWindowFeature
from pyannote.pipeline.parameter import Uniform


class Segmentation(Pipeline):
    """Segmentation pipeline

    Parameters
    ----------
    segmentation : Model, str, or dict, optional
        Pretrained segmentation model. Defaults to "pyannote/segmentation".
        See pyannote.audio.pipelines.utils.get_model for supported format.
    return_activation : bool, optional
        Return soft speaker activation instead of hard segmentation.
        Defaults to False (i.e. hard segmentation).
    inference_kwargs : dict, optional
        Keywords arguments passed to Inference (e.g. batch_size, progress_hook).

    Hyper-parameters
    ----------------
    onset, offset : float
        Onset/offset detection thresholds
    min_duration_on : float
        Remove speaker turn shorter than that many seconds.
    min_duration_off : float
        Fill same-speaker gaps shorter than that many seconds.
    """

    def __init__(
        self,
        segmentation: PipelineModel = "pyannote/segmentation",
        return_activation: bool = False,
        **inference_kwargs,
    ):
        super().__init__()

        self.segmentation = segmentation
        self.return_activation = return_activation

        # load model and send it to GPU (when available and not already on GPU)
        model: Model = get_model(segmentation)
        if model.device.type == "cpu":
            (device,) = get_devices(needs=1)
            model.to(device)

        self.audio_ = model.audio

        inference_kwargs["duration"] = model.specifications.duration
        inference_kwargs["step"] = model.specifications.duration * 0.1
        inference_kwargs["skip_aggregation"] = True

        self.segmentation_inference_ = Inference(model, **inference_kwargs)

        # a speaker is active if its activation is greater than `onset`
        # for at least one frame within a chunk`
        self.activity_threshold = Uniform(0.0, 1.0)

        # mapped speakers activations between two consecutive chunks
        # are said to be consistent if
        self.consistency_threshold = Uniform(0.0, 1.0)

        self.warmup_ratio = Uniform(0.0, 0.4)

        if not self.return_activation:

            #  hyper-parameter used for hysteresis thresholding
            # (in combination with onset := activity_threshold)
            self.offset = Uniform(0.0, 1.0)

            # hyper-parameters used for post-processing i.e. removing short speech turns
            # or filling short gaps between speech turns of one speaker
            self.min_duration_on = Uniform(0.0, 1.0)
            self.min_duration_off = Uniform(0.0, 1.0)

    def initialize(self):
        """Initialize pipeline with current set of parameters"""

        if not self.return_activation:

            self._binarize = Binarize(
                onset=self.activity_threshold,
                offset=self.offset,
                min_duration_on=self.min_duration_on,
                min_duration_off=self.min_duration_off,
            )

    def apply(self, file: AudioFile) -> Annotation:
        """Apply segmentation

        Parameters
        ----------
        file : AudioFile
            Processed file.

        Returns
        -------
        segmentation : `pyannote.core.Annotation`
            Segmentation
        """

        frames: SlidingWindow = self.segmentation_inference_.model.introspection.frames
        raw_activations: SlidingWindowFeature = self.segmentation_inference_(file)
        raw_duration = raw_activations.sliding_window.duration

        activations = warmup_activations(
            raw_activations, warm_up=self.warmup_ratio * raw_duration
        )
        chunks = activations.sliding_window
        activations = split_activations(activations)

        file["@segmentation/raw_activations"] = activations

        num_overlapping_chunks = math.floor(0.5 * chunks.duration / chunks.step)

        # build (chunk, speaker) consistency graph
        #   - (c, s) node indicates that sth speaker of cth chunk is active
        #   - (c, s) == (c+1, s') edge indicates that sth speaker of cth chunk
        #     is mapped to s'th speaker of (c+1)th chunk

        consistency_graph = nx.Graph()

        for current_chunk, current_activation in enumerate(activations):

            for past_chunk in range(
                max(0, current_chunk - num_overlapping_chunks), current_chunk
            ):
                past_activation = activations[past_chunk]
                intersection = past_activation.extent & current_activation.extent

                current_data = current_activation.crop(intersection)
                past_data = past_activation.crop(intersection)
                _, (permutation,), (cost,) = permutate(
                    past_data[np.newaxis], current_data, returns_cost=True
                )

                permutation_cost = np.sum(
                    [
                        cost[past_speaker, current_speaker]
                        for past_speaker, current_speaker in enumerate(permutation)
                    ]
                )

                for past_speaker, current_speaker in enumerate(permutation):

                    # if past speaker is active in the intersection, add it to the graph
                    past_active = (
                        np.max(past_data[:, past_speaker]) > self.activity_threshold
                    )
                    if past_active:
                        consistency_graph.add_node((past_chunk, past_speaker))

                    # if current speaker is active in the intersection, add it to the graph
                    current_active = (
                        np.max(current_data[:, current_speaker])
                        > self.activity_threshold
                    )
                    if current_active:
                        consistency_graph.add_node((current_chunk, current_speaker))

                    # if current speaker is active in both chunks and all chunk activations
                    # are consistent enough, add edge to the graph
                    if (
                        past_active
                        and current_active
                        and permutation_cost < self.consistency_threshold
                    ):
                        consistency_graph.add_edge(
                            (past_chunk, past_speaker), (current_chunk, current_speaker)
                        )

        # bipartite clique graph
        bipartite = nx.algorithms.clique.make_clique_bipartite(consistency_graph)
        is_speaker = nx.get_node_attributes(bipartite, "bipartite")

        aggregated = []
        overlapped = []

        for b, bipartite_component in enumerate(nx.connected_components(bipartite)):

            sub_bipartite = bipartite.subgraph(bipartite_component).copy()

            # a clique is incomplete if it lacks at least one of {num_overlapping_chunks} overlapping chunks
            # remove incomplete cliques as they cannot be trusted
            incomplete_cliques = list(
                filter(
                    lambda clique: (not is_speaker[clique])
                    and (len(sub_bipartite[clique]) < num_overlapping_chunks + 1),
                    sub_bipartite.nodes(),
                )
            )
            sub_bipartite.remove_nodes_from(incomplete_cliques)
            # TODO: corner case for cliques at the beginning of file

            # an orphan is (chunk, speaker) node that is not part of any complete clique.
            # remove orphans as they cannot be trusted
            orphans = [node for node, degree in sub_bipartite.degree() if degree == 0]
            sub_bipartite.remove_nodes_from(orphans)

            complete_cliques = list(
                filter(
                    lambda clique: (not is_speaker[clique])
                    and (len(sub_bipartite[clique]) == num_overlapping_chunks + 1),
                    sub_bipartite.nodes(),
                )
            )
            # TODO: corner case for cliques at the beginning of file

            sub_bipartite_copy = sub_bipartite.copy()
            for clique1, clique2 in combinations(complete_cliques, 2):
                chunk_speaker_in_both_cliques = list(
                    nx.common_neighbors(sub_bipartite_copy, clique1, clique2)
                )
                if len(chunk_speaker_in_both_cliques) == num_overlapping_chunks:
                    sub_bipartite.add_edge(clique1, clique2)

            # (chunk, speaker) components
            components = [
                [n for n in component if is_speaker[n]]
                for component in nx.connected_components(sub_bipartite)
            ]
            num_components = len(components)
            if num_components == 0:
                continue

            num_frames_in_file = frames.samples(
                self.audio_.get_duration(file), mode="center"
            )
            # FIXME -- why do we need this +100 ?
            sub_aggregated = np.zeros((num_frames_in_file + 100, num_components))
            sub_overlapped = np.zeros((num_frames_in_file + 100, num_components))

            for k, component in enumerate(components):

                # aggregate chunks if they belong to the same component
                # remove outermost chunks of each component as their
                for i, (chunk, speaker) in enumerate(sorted(component)):
                    chunk_activations = activations[chunk]
                    speaker_activations: np.ndarray = chunk_activations.data[:, speaker]
                    start_frame = frames.closest_frame(chunk_activations.extent.start)
                    end_frame = start_frame + len(speaker_activations)
                    sub_aggregated[start_frame:end_frame, k] += speaker_activations
                    sub_overlapped[start_frame:end_frame, k] += 1.0

            most_central = np.argmax(sub_overlapped, axis=1)
            for k in range(num_components):
                sub_aggregated[most_central != k, k] = 0.0
                sub_overlapped[most_central != k, k] = 0.0

            # filter skipped components
            active = np.sum(sub_aggregated, axis=0) > 0
            sub_aggregated = sub_aggregated[:, active]
            sub_overlapped = sub_overlapped[:, active]

            aggregated.append(sub_aggregated)
            overlapped.append(sub_overlapped)

        aggregated = np.hstack(aggregated)
        overlapped = np.hstack(overlapped)

        aggregated_activations = SlidingWindowFeature(aggregated / overlapped, frames)

        file["@segmentation/activations"] = aggregated_activations

        if self.return_activation:
            return aggregated_activations

        segmentation = self._binarize(aggregated_activations)
        segmentation.uri = file["uri"]
        return segmentation