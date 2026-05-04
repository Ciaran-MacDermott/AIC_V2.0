# Side-effect import: must run before any ml_package-using module so
# the vendored text_match / xgb_classifier find their NLTK corpora
# locally instead of attempting outbound HTTPS in a walled-garden box.
from api import _nltk_bootstrap  # noqa: F401
