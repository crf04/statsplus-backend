"""A Firebase app one test initializes must not leak into the next (#378 gate).

The pair below runs in file order in one process: the first plants an
initialized app the way ``create_app`` does on a machine with gcloud
application-default credentials, the second must start clean.
"""

from app.utils import firebase_admin as firebase_admin_utils


def test_a_test_may_initialize_the_global_firebase_app():
    firebase_admin_utils._firebase_app = object()


def test_the_next_test_starts_without_an_initialized_firebase_app():
    assert firebase_admin_utils._firebase_app is None
