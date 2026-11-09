"""Layout sentries: where files go, and the helpers that check it.

Other sentry modules import through this package, never another package's private
module.
"""

from tests.sentry.layout._layout import map_paths, section

__all__ = ["map_paths", "section"]
