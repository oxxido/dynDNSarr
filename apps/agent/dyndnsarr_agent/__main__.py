from dyndnsarr_agent.cli import cli, serve

# `main()` sirve el Agent; `python -m dyndnsarr_agent <comando>` admite además los subcomandos.
main = serve

if __name__ == "__main__":
    cli()
