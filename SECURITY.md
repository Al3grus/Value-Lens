# Security

## Reporting a problem

Please report security problems privately, not in a public issue:
[Report a vulnerability](https://github.com/Al3grus/Value-Lens/security/advisories/new).

Useful things to include: what you found, how to reproduce it, and what someone could do with it.

## What is in scope

- The website at https://al3grus.github.io/Value-Lens/ (the only official copy)
- The relay Worker the website uses (`relay/`)
- The `valuelens` command-line tool and Python package

Copies of the site hosted by other people are not covered; report problems there to whoever runs them.

## How the site protects visitors

- Your name and e-mail stay in the browser tab's memory and go only to the SEC, as the SEC requires.
  Neither the page nor the relay stores them.
- The page runs only code from this site and Cloudflare's Turnstile check: its
  Content-Security-Policy blocks everything else. Pyodide (Python in the browser) is checked
  against pinned hashes when the site is built, and the browser checks the numpy and pandas
  downloads against the hashes in Pyodide's lock file.
- The relay forwards only the requests the site needs, only for this site, and only to browsers that
  pass a Cloudflare Turnstile check.
