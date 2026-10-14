def test_packages_importable():
    import dyndnsarr_agent
    import dyndnsarr_hub
    import dyndnsarr_shared

    assert dyndnsarr_shared.__version__ == dyndnsarr_agent.__version__ == dyndnsarr_hub.__version__
