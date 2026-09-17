# Builds the Playwright engine (the one the README recommends) into a
# container.
#
#   docker build -t opensea-scraper .
#   docker run --rm -v "$PWD/out:/out" opensea-scraper \
#     --url "https://opensea.io/collection/boredapeyachtclub" \
#     --pages 3 --out /out/bayc
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential, and .dockerignore
# keeps one out of the build context.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./

# `playwright install chromium`, and unlike a sibling repo that is not a
# compromise. OpenSea barely reads the client: the bundled Chromium was
# served HTTP 200 and the full inlined state on every capture taken for this
# repo, and so was a `curl` announcing itself as `curl/8.x`. (The one
# User-Agent it does refuse is `Python-urllib` — see the README.) Chromium is
# also several hundred MB smaller than Chrome.
#
# `--with-deps` also pulls Chromium's shared-library dependencies through
# apt, which are not pip packages and so cannot ride in requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: three repos in this family
# shipped an image missing proxy_pool.py, which the engine imports at module
# level, so it died with ModuleNotFoundError on every invocation INCLUDING
# `--help` — a broken container that nothing in the repo would have noticed.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     diff_runs.py ./

# Headless here, which is also the default everywhere else in this repo:
# headless and headful were measured IDENTICAL on this site, so a container
# loses nothing. `--headful` is still accepted and needs a display.
ENV OPENSEA_DOCKER=1

ENTRYPOINT ["python3", "playwright_scraper.py", "--headless"]
CMD ["--help"]
