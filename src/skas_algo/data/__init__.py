"""Market-data access for the platform, backed by the sibling skas-data package."""

from .provider import get_available_symbols, get_price_loader

__all__ = ["get_price_loader", "get_available_symbols"]
