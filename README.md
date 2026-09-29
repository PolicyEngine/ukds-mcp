# ukds-mcp

MCP server for the UK Data Service: search the catalogue, manage projects and download microdata.

## Configuration

Catalogue search needs no setup. The server reads the catalogue's public GraphQL key from the UKDS catalogue website's own config, the same way the site's browser client gets it, and reads it again if UKDS rotates it. To pin a key instead, set `UKDS_GRAPHQL_API_KEY`.

Project and download tools need a UK Data Service login. Run the `login` tool once; session cookies are saved to `~/.config/ukds-mcp/session.json` with owner-only permissions.

## Development

```bash
uv run pytest                      # offline tests
UKDS_LIVE_TESTS=1 uv run pytest    # also checks discovery against the live site
```

## License

Code in this repository is released under the [MIT License](LICENSE). Original text and figures are released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) with attribution to PolicyEngine. Third-party data and materials keep their own terms.
