"""Portfolio import — vision (持仓截图 → positions) and CSV (交割单 → knowledge trades/).

Feature 6 of docs/dsa-feature-migration.md. Two independent entry points:

- :func:`doyoutrade.portfolio_import.image_extractor.extract_positions_from_image`
  — vision extraction of positions from a brokerage screenshot via a
  multimodal :class:`~doyoutrade.models.base.ModelAdapter`.
- :func:`doyoutrade.portfolio_import.csv_import.import_trades_csv`
  — broker-statement CSV normalisation into the private knowledge base
  (``trades/<broker>/<YYYY-MM>.csv``), with dedupe on re-import.
"""

from doyoutrade.portfolio_import.csv_import import import_trades_csv
from doyoutrade.portfolio_import.image_extractor import extract_positions_from_image

__all__ = ["extract_positions_from_image", "import_trades_csv"]
