from dyndnsarr_hub.cli import cli, serve

# `main()` sirve el Hub; `python -m dyndnsarr_hub <comando>` admite además los subcomandos.
main = serve

if __name__ == "__main__":
    cli()
