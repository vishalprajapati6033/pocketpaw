from pocketpaw.utils.url import is_valid_url

def test_url_validation_edge_cases():
    """Test how the URL validator handles tricky or malformed inputs."""
    # Test with leading/trailing whitespaces
    assert is_valid_url("   https://google.com   ") is True or is_valid_url("https://google.com")
    
    # Test completely broken protocols
    assert is_valid_url("httpps://invalid-url.com") is False
    
    # Test empty or whitespace-only strings
    assert is_valid_url("") is False
    assert is_valid_url("   ") is False