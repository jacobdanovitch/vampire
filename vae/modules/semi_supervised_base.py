import os
from typing import Dict, List, Optional, Tuple

import numpy as np
from allennlp.common.checks import ConfigurationError
from allennlp.data.vocabulary import Vocabulary
from allennlp.models.model import Model
from allennlp.modules import TokenEmbedder
from allennlp.nn import InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import Average
from overrides import overrides
from tabulate import tabulate
import torch
from torch.nn.functional import log_softmax
from tqdm import tqdm

from vae.common.util import compute_background_log_frequency, load_sparse, read_json
from vae.modules.vae.logistic_normal import LogisticNormal
from scripts.compute_npmi import compute_npmi_during_train, get_files

@Model.register("SemiSupervisedBOW")  # pylint: disable=W0223
class SemiSupervisedBOW(Model):
    """
    Neural variational document-level topic model.
    (https://arxiv.org/abs/1406.5298).

    Parameters
    ----------
    vocab : ``Vocabulary``, required
        A Vocabulary, required in order to compute sizes for input/output projections.
    input_embedder : ``TextFieldEmbedder``, required
        Used to embed the ``tokens`` ``TextField`` we get as input to the model.
    bow_embedder : ``TextFieldEmbedder``, required
        Used to embed the ``tokens`` ``TextField`` we get as input to the model
        into a bag-of-word-counts.
    classification_layer : ``Feedfoward``
        Projection from latent topics to classification logits.
    vae : ``VAE``
        The variational autoencoder used to project the BoW into a latent space.
    alpha: ``float``
        Scales the importance of classification.
    background_data_path: ``str``
        Path to a JSON file containing word frequencies accumulated over the training corpus.
    update_background_freq: ``bool``:
        Whether to allow the background frequency to be learnable.
    track_topics: ``bool``:
        Whether to periodically print the learned topics.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.
    """
    def __init__(self,
                 vocab: Vocabulary,
                 bow_embedder: TokenEmbedder,
                 vae: LogisticNormal,
                 background_data_path: str = None,
                 ref_directory: str = None,
                 kl_weight_annealing: str = None,
                 update_background_freq: bool = True,
                 track_topics: bool = True,
                 apply_batchnorm: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(SemiSupervisedBOW, self).__init__(vocab, regularizer)
        self.metrics = {
                'nkld': Average(),
                'nll': Average(),
                'elbo': Average(),
                'perp': Average()
                }

        self.vocab = vocab
        self.bow_embedder = bow_embedder
        self.vae = vae
        self.track_topics = track_topics
        self.vocab_namespace = "vae"
        self._update_background_freq = update_background_freq
        self._background_freq = self.initialize_bg_from_file(background_data_path)
        self.ref_directory = ref_directory
        if ref_directory is not None:
            self._ref_counts, self._ref_vocab = get_files(ref_directory)
            self._ref_vocab = read_json(self._ref_vocab)
            self._ref_counts = load_sparse(self._ref_counts)
        self._covariates = None

        # Batchnorm to be applied throughout inference.
        self._apply_batchnorm = apply_batchnorm
        vae_vocab_size = self.vocab.get_vocab_size("vae")
        self.bow_bn = torch.nn.BatchNorm1d(vae_vocab_size, eps=0.001, momentum=0.001, affine=True)
        self.bow_bn.weight.data.copy_(torch.ones(vae_vocab_size, dtype=torch.float64))
        self.bow_bn.weight.requires_grad = False

        if kl_weight_annealing == "linear":
            self._kld_weight = min(1, 1 / 50)
        elif kl_weight_annealing == "sigmoid":
            self._kld_weight = float(1/(1 + np.exp(-0.25 * (1 - 15))))
        elif kl_weight_annealing == "constant":
            self._kld_weight = 1.0
        elif kl_weight_annealing is None:
            self._kld_weight = 1.0
        else:
            raise ConfigurationError("anneal type {} not found".format(kl_weight_annealing))

        # Maintain these states for periodically printing topics and updating KLD
        self._metric_epoch_tracker = 0
        self._kl_epoch_tracker = 0
        self._cur_epoch = 0
        self._cur_npmi = np.nan
        initializer(self)

    def initialize_bg_from_file(self, file: str) -> torch.Tensor:
        background_freq = compute_background_log_frequency(self.vocab, self.vocab_namespace, file)
        return torch.nn.Parameter(background_freq, requires_grad=self._update_background_freq)

    @staticmethod
    def bow_reconstruction_loss(reconstructed_bow: torch.Tensor,
                                target_bow: torch.Tensor) -> torch.Tensor:
        # Final shape: (batch, )
        log_reconstructed_bow = log_softmax(reconstructed_bow + 1e-10, dim=-1)
        reconstruction_loss = torch.sum(target_bow * log_reconstructed_bow, dim=-1)
        return reconstruction_loss

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        output = {}
        for metric_name, metric in self.metrics.items():
            if isinstance(metric, float):
                output[metric_name] = metric
            else:
                output[metric_name] = float(metric.get_metric(reset))
        return output

    def update_kld_weight(self, epoch_num: List[int], kl_weight_annealing: str = 'constant') -> None:
        """
        weight annealing scheduler
        """
        _epoch_num = epoch_num[0]
        if _epoch_num != self._kl_epoch_tracker:
            print(self._kld_weight)
            self._kl_epoch_tracker = _epoch_num
            self._cur_epoch += 1
            if kl_weight_annealing == "linear":
                self._kld_weight = min(1, self._cur_epoch / 50)
            elif kl_weight_annealing == "sigmoid":
                self._kld_weight = float(1 / (1 + np.exp(-0.25 * (self._cur_epoch - 15))))
            elif kl_weight_annealing == "constant":
                self._kld_weight = 1.0
            elif kl_weight_annealing is None:
                self._kld_weight = 1.0
            else:
                raise ConfigurationError("anneal type {} not found".format(kl_weight_annealing))

    def compute_custom_metrics_once_per_epoch(self, epoch_num: List[int]) -> None:
        if epoch_num and epoch_num[0] != self._metric_epoch_tracker:

            # Logs the newest set of topics.
            if self.track_topics:
                self.update_topics(epoch_num)

            # Computes NPMI w.r.t the reference directory and saves the value to cur_npmi.
            if self.ref_directory:
                self.update_npmi()

            self._metric_epoch_tracker = epoch_num[0]

    def update_npmi(self) -> None:
        topics = self.extract_topics(self.vae.get_beta())
        mean_npmi = compute_npmi_during_train(topics, self._ref_vocab, self._ref_counts)
        self._cur_npmi = mean_npmi

    def update_topics(self, epoch_num):
        tqdm.write(tabulate(self.extract_topics(self.vae.get_beta()), headers=["Topic #", "Words"]))
        topic_dir = os.path.join(os.path.dirname(self.vocab.serialization_dir), "topics")
        if not os.path.exists(topic_dir):
            os.mkdir(topic_dir)
        ser_dir = os.path.dirname(self.vocab.serialization_dir)
        topic_filepath = os.path.join(ser_dir, "topics", "topics_{}.txt".format(epoch_num[0]))
        with open(topic_filepath, 'w+') as file_:
            file_.write(tabulate(self.extract_topics(self.vae.get_beta()), headers=["Topic #", "Words"]))
        if self._covariates:
            tqdm.write(tabulate(self.extract_topics(self.covariates), headers=["Covariate #", "Words"]))

    def extract_topics(self, weights: torch.Tensor, k: int = 20) -> List[Tuple[str, List[int]]]:
        """
        Given the learned (K, vocabulary size) weights, print the
        top k words from each row as a topic.

        :param weights: ``torch.Tensor``
            The weight matrix whose second dimension equals the vocabulary size.
        :param k: ``int``
            The number of words per topic to display.
        """

        words = list(range(weights.size(1)))
        words = [self.vocab.get_token_from_index(i, "vae") for i in words]

        topics = []

        word_strengths = list(zip(words, self._background_freq.tolist()))
        sorted_by_strength = sorted(word_strengths,
                                    key=lambda x: x[1],
                                    reverse=True)
        background = [x[0] for x in sorted_by_strength][:k]
        topics.append(('bg', background))

        for i, topic in enumerate(weights):
            word_strengths = list(zip(words, topic.tolist()))
            sorted_by_strength = sorted(word_strengths,
                                        key=lambda x: x[1],
                                        reverse=True)
            top_k = [x[0] for x in sorted_by_strength][:k]
            topics.append((str(i), top_k))

        return topics