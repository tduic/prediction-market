"""Sentence embeddings for semantic market matching."""

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class MarketEmbedder:
    """
    Semantic embedder for market titles using sentence transformers.

    Provides embeddings and similarity scoring for matching markets
    based on semantic similarity rather than exact string matching.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """
        Initialize embedder.

        Args:
            model_name: Hugging Face model name for sentence embeddings

        Raises:
            ImportError: If sentence-transformers not installed
        """
        self.model_name = model_name
        # _model is typed Any because sentence_transformers is an optional
        # dependency. When installed it's a SentenceTransformer; when not,
        # it stays None and self._available gates all access.
        self._model: Any = None
        self._available = False

        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(model_name)
            self._available = True
            logger.info(f"Loaded sentence-transformer model: {model_name}")

        except ImportError:
            logger.warning(
                "sentence-transformers not installed. "
                "Semantic matching will not be available. "
                "Install with: pip install sentence-transformers"
            )
        except Exception as e:
            logger.error(
                f"Failed to load sentence-transformer model '{model_name}': {e}"
            )

    def is_available(self) -> bool:
        """Check if embedder is available."""
        return self._available

    def embed(self, text: str) -> np.ndarray | None:
        """
        Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            Embedding as numpy array, or None if unavailable

        Raises:
            ValueError: If text is empty
        """
        if not text or not isinstance(text, str):
            raise ValueError("Text must be non-empty string")

        if not self._available:
            logger.warning("Embedder not available, returning None")
            return None

        try:
            embedding = self._model.encode(text, convert_to_numpy=True)
            return embedding

        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            return None

    def similarity(self, text_a: str, text_b: str) -> float | None:
        """
        Calculate cosine similarity between two texts.

        Args:
            text_a: First text
            text_b: Second text

        Returns:
            Cosine similarity score (0.0 to 1.0), or None if unavailable
        """
        if not self._available:
            return None

        try:
            emb_a = self.embed(text_a)
            emb_b = self.embed(text_b)

            if emb_a is None or emb_b is None:
                return None

            # Cosine similarity
            similarity = float(
                np.dot(emb_a, emb_b) / (np.linalg.norm(emb_a) * np.linalg.norm(emb_b))
            )

            return similarity

        except Exception as e:
            logger.error(f"Error calculating similarity: {e}")
            return None

    def find_matches(
        self, new_market_title: str, existing_titles: list[str], threshold: float = 0.75
    ) -> list[tuple[int, float]]:
        """
        Find matches for a new market among existing markets.

        Args:
            new_market_title: Title of new market
            existing_titles: List of existing market titles to compare against
            threshold: Minimum similarity threshold (0.0 to 1.0)

        Returns:
            List of (index, similarity_score) tuples for matches above threshold,
            sorted by similarity descending. Returns empty list if unavailable.

        Raises:
            ValueError: If threshold not in [0, 1]
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"Threshold must be in [0, 1], got {threshold}")

        if not self._available:
            logger.warning("Embedder not available, returning empty matches")
            return []

        if not existing_titles:
            return []

        try:
            matches = []

            for i, title in enumerate(existing_titles):
                sim = self.similarity(new_market_title, title)

                if sim is not None and sim >= threshold:
                    matches.append((i, sim))

            # Sort by similarity descending
            matches.sort(key=lambda x: x[1], reverse=True)

            return matches

        except Exception as e:
            logger.error(f"Error finding matches for '{new_market_title}': {e}")
            return []

    def batch_embed(
        self, texts: list[str], show_progress: bool = False
    ) -> np.ndarray | None:
        """
        Generate embeddings for multiple texts at once.

        Args:
            texts: List of texts to embed
            show_progress: Whether to show progress bar

        Returns:
            Array of shape (len(texts), embedding_dim), or None if unavailable

        Raises:
            ValueError: If texts is empty
        """
        if not texts:
            raise ValueError("texts list cannot be empty")

        if not self._available:
            return None

        try:
            embeddings = self._model.encode(
                texts, convert_to_numpy=True, show_progress_bar=show_progress
            )
            return embeddings

        except Exception as e:
            logger.error(f"Error batch embedding: {e}")
            return None

    def batch_similarity(self, text_a: str, texts_b: list[str]) -> np.ndarray | None:
        """
        Calculate similarity between one text and multiple texts.

        More efficient than calling similarity() repeatedly.

        Args:
            text_a: Reference text
            texts_b: List of texts to compare against

        Returns:
            Array of similarity scores, or None if unavailable
        """
        if not texts_b:
            raise ValueError("texts_b list cannot be empty")

        if not self._available:
            return None

        try:
            emb_a = self.embed(text_a)
            emb_b = self.batch_embed(texts_b)

            if emb_a is None or emb_b is None:
                return None

            # Cosine similarity: (a · b) / (||a|| * ||b||)
            dot_products = np.dot(emb_b, emb_a)
            norm_a = np.linalg.norm(emb_a)
            norms_b = np.linalg.norm(emb_b, axis=1)

            similarities = dot_products / (norms_b * norm_a)

            return similarities

        except Exception as e:
            logger.error(f"Error batch similarity: {e}")
            return None
