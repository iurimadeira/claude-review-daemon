from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import FROZEN_NOW


@pytest.fixture
def mock_urlopen():
    with patch("bridge.urlopen") as m:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {}
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        m.return_value = mock_resp
        yield m, mock_resp


@pytest.fixture
def frozen_now():
    with (
        patch("run_review.datetime") as rr_dt,
        patch("bridge.datetime") as br_dt,
    ):
        for dt_mock in (rr_dt, br_dt):
            dt_mock.now.return_value = FROZEN_NOW
            dt_mock.side_effect = lambda *a, **kw: datetime(*a, **kw)
        yield FROZEN_NOW
