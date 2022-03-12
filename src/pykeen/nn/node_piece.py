"""Node Piece representations."""

import logging
import pathlib
import pickle
from abc import abstractmethod
from collections import defaultdict
from typing import Callable, Collection, Iterable, Mapping, Optional, Sequence, Tuple, Union

import numpy
import numpy.linalg
import numpy.random
import scipy.sparse
import scipy.sparse.csgraph
import torch
import torch.nn
from class_resolver import ClassResolver, HintOrType, OptionalKwargs
from tqdm.auto import tqdm

from .representation import Representation
from ..constants import AGGREGATIONS, PYKEEN_MODULE
from ..triples import CoreTriplesFactory
from ..triples.splitting import get_absolute_split_sizes, normalize_ratios
from ..typing import MappedTriples, OneOrSequence
from ..utils import broadcast_upgrade_to_sequences, format_relative_comparison

__all__ = [
    "AnchorSelection",
    "DegreeAnchorSelection",
    "MixtureAnchorSelection",
    "PageRankAnchorSelection",
    "RandomAnchorSelection",
    "anchor_selection_resolver",
    "AnchorSearcher",
    "ScipySparseAnchorSearcher",
    "CSGraphAnchorSearcher",
    "anchor_searcher_resolver",
    "Tokenizer",
    "RelationTokenizer",
    "AnchorTokenizer",
    "tokenizer_resolver",
    "NodePieceRepresentation",
]

logger = logging.getLogger(__name__)


class Tokenizer:
    """A base class for tokenizers for NodePiece representations."""

    @abstractmethod
    def __call__(
        self,
        mapped_triples: MappedTriples,
        num_tokens: int,
        num_entities: int,
        num_relations: int,
    ) -> Tuple[int, torch.LongTensor]:
        """
        Tokenize the entities contained given the triples.

        :param mapped_triples: shape: (n, 3)
            the ID-based triples
        :param num_tokens:
            the number of tokens to select for each entity
        :param num_entities:
            the number of entities
        :param num_relations:
            the number of relatiosn

        :return: shape: (num_entities, num_tokens), -1 <= res < vocabulary_size
            the selected relation IDs for each entity. -1 is used as a padding token.
        """
        raise NotImplementedError


class RelationTokenizer(Tokenizer):
    """Tokenize entities by representing them as a bag of relations."""

    def __call__(
        self,
        mapped_triples: MappedTriples,
        num_tokens: int,
        num_entities: int,
        num_relations: int,
    ) -> Tuple[int, torch.LongTensor]:  # noqa: D102
        # tokenize: represent entities by bag of relations
        h, r, t = mapped_triples.t()

        # collect candidates
        e2r = defaultdict(set)
        for e, r in (
            torch.cat(
                [
                    torch.stack([h, r], dim=1),
                    torch.stack([t, r + num_relations], dim=1),
                ],
                dim=0,
            )
            .unique(dim=0)
            .tolist()
        ):
            e2r[e].add(r)

        # randomly sample without replacement num_tokens relations for each entity
        return 2 * num_relations + 1, _random_sample_no_replacement(pool=e2r, num_tokens=num_tokens)


def _edge_index_to_sparse_matrix(
    edge_index: numpy.ndarray,
    num_entities: Optional[int] = None,
) -> scipy.sparse.spmatrix:
    if num_entities is None:
        num_entities = edge_index.max().item() + 1
    return scipy.sparse.coo_matrix(
        (
            numpy.ones_like(edge_index[0], dtype=bool),
            tuple(edge_index),
        ),
        shape=(num_entities, num_entities),
    )


class AnchorSelection:
    """Anchor entity selection strategy."""

    def __init__(self, num_anchors: int = 32) -> None:
        """
        Initialize the strategy.

        :param num_anchors:
            the number of anchor nodes to select.
            # TODO: allow relative
        """
        self.num_anchors = num_anchors

    @abstractmethod
    def __call__(
        self,
        edge_index: numpy.ndarray,
        known_anchors: Optional[numpy.ndarray] = None,
    ) -> numpy.ndarray:
        """
        Select anchor nodes.

        .. note ::
            the number of selected anchors may be smaller than $k$, if there
            are less entities present in the edge index.

        :param edge_index: shape: (m, 2)
            the edge_index, i.e., adjacency list.

        :param known_anchors: numpy.ndarray
            an array of already known anchors for getting only unique anchors

        :return: (k,)
            the selected entity ids
        """
        raise NotImplementedError

    def extra_repr(self) -> Iterable[str]:
        """Extra components for __repr__."""
        yield f"num_anchors={self.num_anchors}"

    def __repr__(self) -> str:  # noqa: D105
        return f"{self.__class__.__name__}({', '.join(self.extra_repr())})"

    def filter_unique(
        self,
        anchor_ranking: numpy.ndarray,
        known_anchors: Optional[numpy.ndarray],
    ) -> numpy.ndarray:
        """
        Filter out already known anchors, and select from remaining ones afterwards.

        .. note ::
            the output size may be smaller, if there are not enough candidates remaining.

        :param anchor_ranking: shape: (n,)
            the anchor node IDs sorted by preference, where the first one is the most preferrable.
        :param known_anchors: shape: (m,)
            a collection of already known anchors

        :return: shape: (m + num_anchors,)
            the extended anchors, i.e., the known ones and `num_anchors` novel ones.
        """
        if known_anchors is None:
            return anchor_ranking[: self.num_anchors]

        # isin() preserves the sorted order
        unique_anchors = anchor_ranking[~numpy.isin(anchor_ranking, known_anchors)]
        unique_anchors = unique_anchors[: self.num_anchors]
        return numpy.concatenate([known_anchors, unique_anchors])


class SingleSelection(AnchorSelection):
    """Single-step selection."""

    def __call__(
        self,
        edge_index: numpy.ndarray,
        known_anchors: Optional[numpy.ndarray] = None,
    ) -> numpy.ndarray:
        """
        Select anchor nodes.

        .. note ::
            the number of selected anchors may be smaller than $k$, if there
            are less entities present in the edge index.

        :param edge_index: shape: (m, 2)
            the edge_index, i.e., adjacency list.

        :param known_anchors: numpy.ndarray
            an array of already known anchors for getting only unique anchors

        :return: (k,)
            the selected entity ids
        """
        return self.filter_unique(anchor_ranking=self.rank(edge_index=edge_index), known_anchors=known_anchors)

    @abstractmethod
    def rank(self, edge_index: numpy.ndarray) -> numpy.ndarray:
        """
        Rank nodes.

        :param edge_index: shape: (m, 2)
            the edge_index, i.e., adjacency list.

        :return: (n,)
            the node IDs sorted decreasingly by anchor selection preference.
        """
        raise NotImplementedError


class DegreeAnchorSelection(SingleSelection):
    """Select entities according to their (undirected) degree."""

    def rank(self, edge_index: numpy.ndarray) -> numpy.ndarray:  # noqa: D102
        unique, counts = numpy.unique(edge_index, return_counts=True)
        # sort by decreasing degree
        ids = numpy.argsort(counts)[::-1]
        return unique[ids]


def page_rank(
    edge_index: numpy.ndarray,
    max_iter: int = 1_000,
    alpha: float = 0.05,
    epsilon: float = 1.0e-04,
) -> numpy.ndarray:
    """
    Compute page-rank vector by power iteration.

    :param edge_index: shape: (2, m)
        the edge index of the graph, i.e, the edge list.
    :param max_iter: $>0$
        the maximum number of iterations
    :param alpha: $0 < x < 1$
        the smoothing value / teleport probability
    :param epsilon: $>0$
        a (small) constant to check for convergence

    :return: shape: (n,)
        the page-rank vector, i.e., a score between 0 and 1 for each node.
    """
    # convert to sparse matrix
    adj = _edge_index_to_sparse_matrix(edge_index=edge_index)
    # symmetrize
    # TODO: should we add self-links
    # adj = (adj + adj.transpose() + scipy.sparse.eye(m=adj.shape[0], format="coo")).tocsr()
    adj = (adj + adj.transpose()).tocsr()
    # degree for adjacency normalization
    degree_inv = numpy.reciprocal(numpy.asarray(adj.sum(axis=0), dtype=float))[0]
    n = degree_inv.shape[0]
    # power iteration
    x = numpy.full(shape=(n,), fill_value=1.0 / n)
    x_old = x
    beta = 1.0 - alpha
    for i in range(max_iter):
        x = beta * adj.dot(degree_inv * x) + alpha / n
        if numpy.linalg.norm(x - x_old, ord=float("+inf")) < epsilon:
            logger.debug(f"Converged after {i} iterations up to {epsilon}.")
            break
        x_old = x
    else:  # for/else, cf. https://book.pythontips.com/en/latest/for_-_else.html
        logger.warning(f"No covergence after {max_iter} iterations with epsilon={epsilon}.")
    return x


class PageRankAnchorSelection(SingleSelection):
    """Select entities according to their page rank."""

    def __init__(
        self,
        num_anchors: int = 32,
        max_iter: int = 1_000,
        alpha: float = 0.05,
        epsilon: float = 1.0e-04,
    ) -> None:
        """
        Initialize the selection strategy.

        :param num_anchors:
            the number of anchors to select
        :param max_iter:
            the maximum number of power iterations
        :param alpha:
            the smoothing value / teleport probability
        :param epsilon:
            a constant to check for convergence
        """
        super().__init__(num_anchors=num_anchors)
        self.max_iter = max_iter
        self.alpha = alpha
        self.epsilon = epsilon

    def extra_repr(self) -> Iterable[str]:  # noqa: D102
        yield from super().extra_repr()
        yield f"max_iter={self.max_iter}"
        yield f"alpha={self.alpha}"
        yield f"epsilon={self.epsilon}"

    def rank(self, edge_index: numpy.ndarray) -> numpy.ndarray:  # noqa: D102
        # sort by decreasing page rank
        return numpy.argsort(
            page_rank(
                edge_index=edge_index,
                max_iter=self.max_iter,
                alpha=self.alpha,
                epsilon=self.epsilon,
            ),
        )[::-1]


class RandomAnchorSelection(SingleSelection):
    """Random node selection."""

    def __init__(
        self,
        num_anchors: int = 32,
        random_seed: Optional[int] = None,
    ) -> None:
        """
        Initialize the selection stragegy.

        :param num_anchors:
            the number of anchors to select
        :param random_seed:
            the random seed to use.
        """
        super().__init__(num_anchors=num_anchors)
        self.generator: numpy.random.Generator = numpy.random.default_rng(random_seed)

    def rank(self, edge_index: numpy.ndarray) -> numpy.ndarray:  # noqa: D102
        return self.generator.permutation(edge_index.max())


class MixtureAnchorSelection(AnchorSelection):
    """A weighted mixture of different anchor selection strategies."""

    def __init__(
        self,
        selections: Sequence[HintOrType[AnchorSelection]],
        ratios: Union[None, float, Sequence[float]] = None,
        selections_kwargs: OneOrSequence[OptionalKwargs] = None,
        **kwargs,
    ) -> None:
        """
        Initialize the selection strategy.

        :param selections:
            the individual selections.
            For the sake of selecting unique anchors, selections will be executed in the given order
            eg, ['degree', 'pagerank'] will be executed differently from ['pagerank', 'degree']
        :param ratios:
            the ratios, cf. normalize_ratios. None means uniform ratios
        :param selection_kwargs:
            additional keyword-based arguments for the individual selection strategies
        :param kwargs:
            additional keyword-based arguments passed to AnchorSelection.__init__,
            in particular, the total number of anchors.
        """
        super().__init__(**kwargs)
        n_selections = len(selections)
        # input normalization
        if selections_kwargs is None:
            selections_kwargs = [None] * n_selections
        if ratios is None:
            ratios = numpy.ones(shape=(n_selections,)) / n_selections
        # determine absolute number of anchors for each strategy
        num_anchors = get_absolute_split_sizes(n_total=self.num_anchors, ratios=normalize_ratios(ratios=ratios))
        self.selections = [
            anchor_selection_resolver.make(selection, selection_kwargs, num_anchors=num)
            for selection, selection_kwargs, num in zip(selections, selections_kwargs, num_anchors)
        ]
        # if pre-instantiated
        for selection, num in zip(self.selections, num_anchors):
            if selection.num_anchors != num:
                logger.warning(f"{selection} had wrong number of anchors. Setting to {num}")
                selection.num_anchors = num

    def extra_repr(self) -> Iterable[str]:  # noqa: D102
        yield from super().extra_repr()
        yield f"selections={self.selections}"

    def __call__(
        self,
        edge_index: numpy.ndarray,
        known_anchors: Optional[numpy.ndarray] = None,
    ) -> numpy.ndarray:  # noqa: D102
        anchors = known_anchors or None
        for selection in self.selections:
            anchors = selection(edge_index=edge_index, known_anchors=anchors)
        return anchors


anchor_selection_resolver: ClassResolver[AnchorSelection] = ClassResolver.from_subclasses(
    base=AnchorSelection,
    default=DegreeAnchorSelection,
    skip={SingleSelection},
)


class AnchorSearcher:
    """A method for finding the closest anchors."""

    @abstractmethod
    def __call__(self, edge_index: numpy.ndarray, anchors: numpy.ndarray, k: int) -> numpy.ndarray:
        """
        Find the $k$ closest anchor nodes for each entity.

        :param edge_index: shape: (2, m)
            the edge index
        :param anchors: shape: (a,)
            the selected anchor entity Ids
        :param k:
            the number of closest anchors to return

        :return: shape: (n, k), -1 <= res < a
            the Ids of the closest anchors
        """
        raise NotImplementedError

    def extra_repr(self) -> Iterable[str]:
        """Extra components for __repr__."""
        return []

    def __repr__(self) -> str:  # noqa: D105
        return f"{self.__class__.__name__}({', '.join(self.extra_repr())})"


class CSGraphAnchorSearcher(AnchorSearcher):
    """Find closest anchors using scipy.sparse.csgraph."""

    def __call__(self, edge_index: numpy.ndarray, anchors: numpy.ndarray, k: int) -> numpy.ndarray:  # noqa: D102
        # convert to adjacency matrix
        adjacency = _edge_index_to_sparse_matrix(edge_index=edge_index).tocsr()
        # compute distances between anchors and all nodes, shape: (num_anchors, num_entities)
        distances = scipy.sparse.csgraph.shortest_path(
            csgraph=adjacency,
            directed=False,
            return_predecessors=False,
            unweighted=True,
            indices=anchors,
        )
        # select anchor IDs with smallest distance
        return torch.as_tensor(
            numpy.argpartition(distances, kth=min(k, distances.shape[0]), axis=0)[:k, :].T,
            dtype=torch.long,
        )


class ScipySparseAnchorSearcher(AnchorSearcher):
    """Find closest anchors using scipy.sparse."""

    def __init__(self, max_iter: int = 5) -> None:
        """
        Initialize the searcher.

        :param max_iter:
            the maximum number of hops to consider
        """
        self.max_iter = max_iter

    def extra_repr(self) -> Iterable[str]:  # noqa: D102
        yield from super().extra_repr()
        yield f"max_iter={self.max_iter}"

    @staticmethod
    def create_adjacency(
        edge_index: numpy.ndarray,
    ) -> scipy.sparse.spmatrix:
        """
        Create a sparse adjacency matrix from a given edge index.

        :param edge_index: shape: (2, m)
            the edge index

        :return: shape: (n, n)
            a square sparse adjacency matrix
        """
        # infer shape
        num_entities = edge_index.max().item() + 1
        # create adjacency matrix
        adjacency = scipy.sparse.coo_matrix(
            (
                numpy.ones_like(edge_index[0], dtype=bool),
                tuple(edge_index),
            ),
            shape=(num_entities, num_entities),
            dtype=bool,
        )
        # symmetric + self-loops
        adjacency = adjacency + adjacency.transpose() + scipy.sparse.eye(num_entities, dtype=bool, format="coo")
        adjacency = adjacency.tocsr()
        logger.debug(
            f"Created sparse adjacency matrix of shape {adjacency.shape} where "
            f"{format_relative_comparison(part=adjacency.nnz, total=numpy.prod(adjacency.shape))} "
            f"are non-zero entries.",
        )
        return adjacency

    @staticmethod
    def bfs(
        anchors: numpy.ndarray,
        adjacency: scipy.sparse.spmatrix,
        max_iter: int,
        k: int,
    ) -> numpy.ndarray:
        """
        Determine the candidate pool using breadth-first search.

        :param anchors: shape: (a,)
            the anchor node IDs
        :param adjacency: shape: (n, n)
            the adjacency matrix
        :param max_iter:
            the maximum number of hops to consider
        :param k:
            the minimum number of anchor nodes to reach

        :return: shape: (n, a)
            a boolean array indicating whether anchor $j$ is in the set of $k$ closest anchors for node $i$
        """
        num_entities = adjacency.shape[0]
        # for each entity, determine anchor pool by BFS
        num_anchors = len(anchors)

        # an array storing whether node i is reachable by anchor j
        reachable = numpy.zeros(shape=(num_entities, num_anchors), dtype=bool)
        reachable[anchors] = numpy.eye(num_anchors, dtype=bool)

        # an array indicating whether a node is closed, i.e., has found at least $k$ anchors
        final = numpy.zeros(shape=(num_entities,), dtype=bool)

        # the output
        pool = numpy.zeros(shape=(num_entities, num_anchors), dtype=bool)

        # TODO: take all (q-1) hop neighbors before selecting from q-hop
        old_reachable = reachable
        for i in range(max_iter):
            # propagate one hop
            reachable = adjacency.dot(reachable)
            # convergence check
            if (reachable == old_reachable).all():
                logger.warning(f"Search converged after iteration {i} without all nodes being reachable.")
                break
            old_reachable = reachable
            # copy pool if we have seen enough anchors and have not yet stopped
            num_reachable = reachable.sum(axis=1)
            enough = num_reachable >= k
            mask = enough & ~final
            logger.debug(
                f"Iteration {i}: {format_relative_comparison(enough.sum(), total=num_entities)} closed nodes.",
            )
            pool[mask] = reachable[mask]
            # stop once we have enough
            final |= enough
            if final.all():
                break
        return pool

    @staticmethod
    def select(
        pool: numpy.ndarray,
        k: int,
    ) -> numpy.ndarray:
        """
        Select $k$ anchors from the given pools.

        :param pool: shape: (n, a)
            the anchor candidates for each node (a binary array)
        :param k:
            the number of candidates to select

        :return: shape: (n, k)
            the selected anchors. May contain -1 if there is an insufficient number of  candidates
        """
        tokens = numpy.full(shape=(pool.shape[0], k), fill_value=-1, dtype=int)
        generator = numpy.random.default_rng()
        # TODO: can we replace this loop with something vectorized?
        for i, row in enumerate(pool):
            (this_pool,) = row.nonzero()
            chosen = generator.choice(a=this_pool, size=min(k, this_pool.size), replace=False, shuffle=False)
            tokens[i, : len(chosen)] = chosen
        return tokens

    def __call__(self, edge_index: numpy.ndarray, anchors: numpy.ndarray, k: int) -> numpy.ndarray:  # noqa: D102
        adjacency = self.create_adjacency(edge_index=edge_index)
        pool = self.bfs(anchors=anchors, adjacency=adjacency, max_iter=self.max_iter, k=k)
        return self.select(pool=pool, k=k)


# TODO: use graph library, such as igraph, graph-tool, or networkit
anchor_searcher_resolver: ClassResolver[AnchorSearcher] = ClassResolver.from_subclasses(
    base=AnchorSearcher,
    default=CSGraphAnchorSearcher,
)


class AnchorTokenizer(Tokenizer):
    """
    Tokenize entities by representing them as a bag of anchor entities.

    The entities are chosen by shortest path distance.
    """

    def __init__(
        self,
        selection: HintOrType[AnchorSelection] = None,
        selection_kwargs: OptionalKwargs = None,
        searcher: HintOrType[AnchorSearcher] = None,
        searcher_kwargs: OptionalKwargs = None,
    ) -> None:
        """
        Initialize the tokenizer.

        :param selection:
            the anchor node selection strategy.
        :param selection_kwargs:
            additional keyword-based arguments passed to the selection strategy
        :param searcher:
            the component for searching the closest anchors for each entity
        :param searcher_kwargs:
            additional keyword-based arguments passed to the searcher
        """
        self.anchor_selection = anchor_selection_resolver.make(selection, pos_kwargs=selection_kwargs)
        self.searcher = anchor_searcher_resolver.make(searcher, pos_kwargs=searcher_kwargs)

    def __call__(
        self,
        mapped_triples: MappedTriples,
        num_tokens: int,
        num_entities: int,
        num_relations: int,
    ) -> torch.LongTensor:  # noqa: D102
        edge_index = mapped_triples[:, [0, 2]].numpy().T
        # select anchors
        logger.info(f"Selecting anchors according to {self.anchor_selection}")
        anchors = self.anchor_selection(edge_index=edge_index)
        if len(numpy.unique(anchors)) < len(anchors):
            logger.warning(f"Only {len(numpy.unique(anchors))} out of {len(anchors)} anchors are unique")
        # find closest anchors
        logger.info(f"Searching closest anchors with {self.searcher}")
        tokens = self.searcher(edge_index=edge_index, anchors=anchors, k=num_tokens)
        num_empty = (tokens < 0).all(axis=1).sum()
        if num_empty > 0:
            logger.warning(
                f"{format_relative_comparison(part=num_empty, total=num_entities)} " f"do not have any anchor.",
            )
        # convert to torch
        return len(anchors) + 1, torch.as_tensor(tokens, dtype=torch.long)


def _random_sample_no_replacement(
    pool: Mapping[int, Collection[int]],
    num_tokens: int,
) -> torch.LongTensor:
    # randomly sample without replacement num_tokens relations for each entity
    assignment = torch.full(
        size=(len(pool), num_tokens),
        dtype=torch.long,
        fill_value=-1,
    )
    # TODO: vectorization?
    for idx, this_pool in tqdm(pool.items(), desc="sampling", leave=False, unit_scale=True):
        this_pool_t = torch.as_tensor(data=list(this_pool), dtype=torch.long)
        this_pool = this_pool_t[torch.randperm(this_pool_t.shape[0])[:num_tokens]]
        assignment[idx, : len(this_pool_t)] = this_pool
    return assignment


class PrecomputedTokenizerLoader:
    """A loader for precomputed tokenization."""

    @abstractmethod
    def __call__(self, path: pathlib.Path) -> Tuple[Mapping[int, Collection[int]], int]:
        """Load tokenization from the given path."""
        raise NotImplementedError


class GalkinPickleLoader(PrecomputedTokenizerLoader):
    """
    A loader for pickle files provided by Galkin et al.

    .. seealso ::
        https://github.com/migalkin/NodePiece/blob/9adc57efe302919d017d74fc648f853308cf75fd/download_data.sh
        https://github.com/migalkin/NodePiece/blob/9adc57efe302919d017d74fc648f853308cf75fd/ogb/download.sh
    """

    def __call__(self, path: pathlib.Path) -> Tuple[Mapping[int, Collection[int]], int]:  # noqa: D102
        with path.open(mode="rb") as pickle_file:
            # contains: anchor_ids, entity_ids, mapping {entity_id -> {"ancs": anchors, "dists": distances}}
            anchor_ids, mapping = pickle.load(pickle_file)[0::2]
        logger.info(f"Loaded precomputed pools with {len(anchor_ids)} anchors, and {len(mapping)} pools.")
        # normalize anchor_ids
        anchor_map = {a: i for i, a in enumerate(anchor_ids) if a >= 0}
        # cf. https://github.com/pykeen/pykeen/pull/822#discussion_r822889541
        # TODO: keep distances?
        return {
            key: [anchor_map[a] for a in value["ancs"] if a in anchor_map]
            for key, value in tqdm(mapping.items(), desc="ID Mapping", unit_scale=True, leave=False)
        }, len(anchor_map)


precomputed_tokenizer_loader_resolver: ClassResolver[PrecomputedTokenizerLoader] = ClassResolver.from_subclasses(
    base=PrecomputedTokenizerLoader,
    default=GalkinPickleLoader,
)


class PrecomputedPoolTokenizer(Tokenizer):
    """A tokenizer using externally precomputed tokenization."""

    @classmethod
    def _load_pool(
        cls,
        *,
        path: Optional[pathlib.Path] = None,
        url: Optional[str] = None,
        download_kwargs: OptionalKwargs = None,
        pool: Optional[Mapping[int, Collection[int]]] = None,
        loader: HintOrType[PrecomputedTokenizerLoader] = None,
    ) -> Tuple[Mapping[int, Collection[int]], int]:
        """Load a precomputed pool via one of the supported ways."""
        if pool is not None:
            return pool, max(c for candidates in pool.values() for c in candidates) + 1 + 1  # +1 for padding
        if url is not None and path is None:
            module = PYKEEN_MODULE.submodule(__name__, tokenizer_resolver.normalize_cls(cls=cls))
            path = module.ensure(url=url, download_kwargs=download_kwargs)
        if path is None:
            raise ValueError("Must provide at least one of pool, path, or url.")

        if not path.is_file():
            raise FileNotFoundError(path)
        logger.info(f"Loading precomputed pools from {path}")
        return precomputed_tokenizer_loader_resolver.make(loader)(path=path)

    def __init__(
        self,
        *,
        path: Optional[pathlib.Path] = None,
        url: Optional[str] = None,
        download_kwargs: OptionalKwargs = None,
        pool: Optional[Mapping[int, Collection[int]]] = None,
        randomize_selection: bool = False,
    ):
        """
        Initialize the tokenizer.

        .. note ::
            the preference order for loading the precomputed pools is (1) from the given pool (2) from the given path,
            and (3) by downloading from the given url

        :param path:
            a path for a file containing the precomputed pools
        :param url:
            an url to download the file with precomputed pools from
        :param download_kwargs:
            additional download parameters, passed to pystow.Module.ensure
        :param pool:
            the precomputed pools.
        :param randomize_selection:
            whether to randomly choose from tokens, or always take the first `num_token` precomputed tokens.

        """
        self.pool, self.vocabulary_size = self._load_pool(
            path=path, url=url, pool=pool, download_kwargs=download_kwargs
        )
        # verify pool
        if set(self.pool.keys()) != set(range(len(self.pool))):
            raise ValueError("Expected pool to contain keys 0...(N-1)")
        self.randomize_selection = randomize_selection

    def __call__(
        self, mapped_triples: MappedTriples, num_tokens: int, num_entities: int, num_relations: int
    ) -> Tuple[int, torch.LongTensor]:  # noqa: D102
        if num_entities != len(self.pool):
            raise ValueError(f"Invalid number of entities ({num_entities}); expected {len(self.pool)}")
        if self.randomize_selection:
            assignment = _random_sample_no_replacement(pool=self.pool, num_tokens=num_tokens)
        else:
            # choose first num_tokens
            assignment = torch.full(
                size=(len(self.pool), num_tokens),
                dtype=torch.long,
                fill_value=-1,
            )
            # TODO: vectorization?
            for idx, this_pool in self.pool.items():
                this_pool_t = torch.as_tensor(data=list(this_pool)[:num_tokens], dtype=torch.long)
                assignment[idx, : len(this_pool_t)] = this_pool_t
        return self.vocabulary_size, assignment


tokenizer_resolver: ClassResolver[Tokenizer] = ClassResolver.from_subclasses(
    base=Tokenizer,
    default=RelationTokenizer,
)


def resolve_aggregation(
    aggregation: Union[None, str, Callable[[torch.FloatTensor, int], torch.FloatTensor]],
) -> Callable[[torch.FloatTensor, int], torch.FloatTensor]:
    """
    Resolve the aggregation function.

    .. warning ::
        This function does *not* check whether torch.<aggregation> is a method which is a valid aggregation.

    :param aggregation:
        the aggregation choice. Can be either
        1. None, in which case the torch.mean is returned
        2. a string, in which case torch.<aggregation> is returned
        3. a callable, which is returned without change

    :return:
        the chosen aggregation function.
    """
    if aggregation is None:
        return torch.mean

    if isinstance(aggregation, str):
        if aggregation not in AGGREGATIONS:
            logger.warning(
                f"aggregation={aggregation} is not one of the predefined ones ({sorted(AGGREGATIONS.keys())}).",
            )
        return getattr(torch, aggregation)

    return aggregation


class TokenizationRepresentation(Representation):
    """A module holding the result of tokenization."""

    #: the token ID of the padding token
    vocabulary_size: int

    #: the token representations
    vocabulary: Representation

    #: the assigned tokens for each entity
    assignment: torch.LongTensor

    def __init__(
        self,
        assignment: torch.LongTensor,
        token_representation: HintOrType[Representation] = None,
        token_representation_kwargs: OptionalKwargs = None,
        **kwargs,
    ) -> None:
        """
        Initialize the tokenization.

        :param assignment: shape: `(n, num_chosen_tokens)`
            the token assignment.
        :param token_representation: shape: `(num_total_tokens, *shape)`
            the token representations
        :param token_representation_kwargs:
            additional keyword-based parameters
        :param kwargs:
            additional keyword-based parameters passed to super.__init__
        """
        # needs to be lazily imported to avoid cyclic imports
        from . import representation_resolver

        # fill padding (nn.Embedding cannot deal with negative indices)
        padding = assignment < 0
        # sometimes, assignment.max() does not cover all relations (eg, inductive inference graphs
        # contain a subset of training relations) - for that, the padding index is the last index of the Representation
        self.vocabulary_size = (
            token_representation.max_id
            if isinstance(token_representation, Representation)
            else assignment.max().item() + 2  # exclusive (+1) and including padding (+1)
        )

        assignment[padding] = self.vocabulary_size - 1  # = assignment.max().item() + 1
        max_id, num_chosen_tokens = assignment.shape

        # resolve token representation
        token_representation = representation_resolver.make(
            token_representation,
            token_representation_kwargs,
            max_id=self.vocabulary_size,
        )
        super().__init__(max_id=max_id, shape=(num_chosen_tokens,) + token_representation.shape, **kwargs)

        # input validation
        if token_representation.max_id < self.vocabulary_size:
            raise ValueError(
                f"The token representations only contain {token_representation.max_id} representations,"
                f"but there are {self.vocabulary_size} tokens in use.",
            )
        elif token_representation.max_id > self.vocabulary_size:
            logger.warning(
                f"Token representations do contain more representations ({token_representation.max_id}) "
                f"than tokens are used ({self.vocabulary_size}).",
            )
        # register as buffer
        self.register_buffer(name="assignment", tensor=assignment)
        # assign sub-module
        self.vocabulary = token_representation

    @classmethod
    def from_tokenizer(
        cls,
        tokenizer: Tokenizer,
        num_tokens: int,
        mapped_triples: MappedTriples,
        num_entities: int,
        num_relations: int,
        token_representation: HintOrType[Representation] = None,
        token_representation_kwargs: OptionalKwargs = None,
        **kwargs,
    ) -> "TokenizationRepresentation":
        """
        Create a tokenization from applying a tokenizer.

        :param tokenizer:
            the tokenizer instance.
        :param num_tokens:
            the number of tokens to select for each entity.
        :param token_representation:
            the pre-instantiated token representations, or an EmbeddingSpecification to create them
        :param mapped_triples:
            the ID-based triples
        :param num_entities:
            the number of entities
        :param num_relations:
            the number of relations
        :param kwargs:
            additional keyword-based parameters passed to TokenizationRepresentation.__init__
        """
        # apply tokenizer
        vocabulary_size, assignment = tokenizer(
            mapped_triples=mapped_triples,
            num_tokens=num_tokens,
            num_entities=num_entities,
            num_relations=num_relations,
        )
        return TokenizationRepresentation(
            assignment=assignment,
            token_representation=token_representation,
            token_representation_kwargs=token_representation_kwargs,
            **kwargs,
        )

    def extra_repr(self) -> str:  # noqa: D102
        return "\n".join(
            (
                f"max_id={self.assignment.shape[0]},",
                f"num_tokens={self.assignment.shape[1]},",
                f"vocabulary_size={self.vocabulary_size},",
            )
        )

    def _plain_forward(
        self,
        indices: Optional[torch.LongTensor] = None,
    ) -> torch.FloatTensor:  # noqa: D102
        # get token IDs, shape: (*, num_chosen_tokens)
        token_ids = self.assignment
        if indices is not None:
            token_ids = token_ids[indices]

        # lookup token representations, shape: (*, num_chosen_tokens, *shape)
        return self.vocabulary(token_ids)


class NodePieceRepresentation(Representation):
    r"""
    Basic implementation of node piece decomposition [galkin2021]_.

    .. math ::
        x_e = agg(\{T[t] \mid t \in tokens(e) \})

    where $T$ are token representations, $tokens$ selects a fixed number of $k$ tokens for each entity, and $agg$ is
    an aggregation function, which aggregates the individual token representations to a single entity representation.

    .. note ::
        This implementation currently only supports representation of entities by bag-of-relations.
    """

    #: the token representations
    token_representations: Sequence[TokenizationRepresentation]

    def __init__(
        self,
        *,
        triples_factory: CoreTriplesFactory,
        token_representations: OneOrSequence[HintOrType[Representation]] = None,
        token_representation_kwargs: OneOrSequence[OptionalKwargs] = None,
        tokenizers: OneOrSequence[HintOrType[Tokenizer]] = None,
        tokenizers_kwargs: OneOrSequence[OptionalKwargs] = None,
        num_tokens: OneOrSequence[int] = 2,
        aggregation: Union[None, str, Callable[[torch.FloatTensor, int], torch.FloatTensor]] = None,
        max_id: Optional[int] = None,
        shape: Optional[Sequence[int]] = None,
        **kwargs,
    ):
        """
        Initialize the representation.

        :param triples_factory:
            the triples factory
        :param token_representations:
            the token representation specification, or pre-instantiated representation module.
        :param tokenizers:
            the tokenizer to use, cf. `pykeen.nn.node_piece.tokenizer_resolver`.
        :param tokenizers_kwargs:
            additional keyword-based parameters passed to the tokenizer upon construction.
        :param num_tokens:
            the number of tokens for each entity.
        :param aggregation:
            aggregation of multiple token representations to a single entity representation. By default,
            this uses :func:`torch.mean`. If a string is provided, the module assumes that this refers to a top-level
            torch function, e.g. "mean" for :func:`torch.mean`, or "sum" for func:`torch.sum`. An aggregation can
            also have trainable parameters, .e.g., ``MLP(mean(MLP(tokens)))`` (cf. DeepSets from [zaheer2017]_). In
            this case, the module has to be created outside of this component.

            We could also have aggregations which result in differently shapes output, e.g. a concatenation of all
            token embeddings resulting in shape ``(num_tokens * d,)``. In this case, `shape` must be provided.

            The aggregation takes two arguments: the (batched) tensor of token representations, in shape
            ``(*, num_tokens, *dt)``, and the index along which to aggregate.
        :param kwargs:
            additional keyword-based parameters passed to super.__init__
        """
        if max_id:
            assert max_id == triples_factory.num_entities

        # normalize triples
        mapped_triples = triples_factory.mapped_triples
        if triples_factory.create_inverse_triples:
            # inverse triples are created afterwards implicitly
            mapped_triples = mapped_triples[mapped_triples[:, 1] < triples_factory.real_num_relations]

        token_representations, token_representation_kwargs, num_tokens = broadcast_upgrade_to_sequences(
            token_representations, token_representation_kwargs, num_tokens
        )

        # tokenize
        token_representations = [
            TokenizationRepresentation.from_tokenizer(
                tokenizer=tokenizer_inst,
                num_tokens=num_tokens_,
                token_representation=token_representation,
                token_representation_kwargs=token_representation_kwargs,
                mapped_triples=mapped_triples,
                num_entities=triples_factory.num_entities,
                num_relations=triples_factory.real_num_relations,
            )
            for tokenizer_inst, token_representation, token_representation_kwargs, num_tokens_ in zip(
                tokenizer_resolver.make_many(queries=tokenizers, kwargs=tokenizers_kwargs),
                token_representations,
                token_representation_kwargs,
                num_tokens,
            )
        ]

        # determine shape
        if shape is None:
            shapes = {t.vocabulary.shape for t in token_representations}
            if len(shapes) != 1:
                raise ValueError(f"Inconsistent token shapes: {shapes}")
            shape = list(shapes)[0]

        # super init; has to happen *before* any parameter or buffer is assigned
        super().__init__(max_id=triples_factory.num_entities, shape=shape, **kwargs)

        # assign module
        self.token_representations = torch.nn.ModuleList(token_representations)

        # Assign default aggregation
        self.aggregation = resolve_aggregation(aggregation=aggregation)
        self.aggregation_index = -(1 + len(shape))

    def extra_repr(self) -> str:  # noqa: D102
        aggregation_str = self.aggregation.__name__ if hasattr(self.aggregation, "__name__") else str(self.aggregation)
        return f"aggregation={aggregation_str}, "

    def _plain_forward(
        self,
        indices: Optional[torch.LongTensor] = None,
    ) -> torch.FloatTensor:  # noqa: D102
        return self.aggregation(
            torch.cat(
                [tokenization(indices=indices) for tokenization in self.token_representations],
                dim=self.aggregation_index,
            ),
            self.aggregation_index,
        )