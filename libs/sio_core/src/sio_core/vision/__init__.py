"""Shared vision adapters.

Only what more than one service needs lives here. Detectors belong to ``services/perception``,
trackers to ``services/tracking``; the embedder is here because ``perception`` embeds frames and
``worldmodel`` embeds queries, and if the two used different models the vectors would be
incomparable.
"""

from .clip_embedder import HashEmbedder, OnnxClipEmbedder

__all__ = ["HashEmbedder", "OnnxClipEmbedder"]
