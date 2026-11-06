def main(argv=None):
    import sys

    from wings_x import cli

    if argv is None:
        argv = sys.argv[1:]
    sys.exit(cli.main(argv))


if __name__ == "__main__":
    main()
