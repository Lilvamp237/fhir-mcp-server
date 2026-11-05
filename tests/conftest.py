import pytest
from unittest.mock import Mock, patch

@pytest.fixture(autouse=True, scope="session")
def patch_webbrowser_open():
    # Sign-in flows (for example an Epic login) may open a browser; tests never should.
    with patch('webbrowser.open', new=Mock()), \
            patch('webbrowser.open_new', new=Mock()), \
            patch('webbrowser.open_new_tab', new=Mock()):
        yield
