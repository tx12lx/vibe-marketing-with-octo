from __future__ import annotations

import io
import logging
import re
from typing import Optional

import pdfplumber
import requests
import urllib3

import google.auth
import google.auth.transport.requests

_log = logging.getLogger(__name__)

# Corporate SSL inspection proxies replace certificates with company-signed ones
# that Python's bundled CA store doesn't trust. Disable verification to match
# browser behaviour on the same network.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_GDOC_PATTERN = re.compile(r"docs\.google\.com/document/d/([^/?#]+)")
_GSHEET_PATTERN = re.compile(r"docs\.google\.com/spreadsheets/d/([^/?#]+)")
_GDRIVE_PATTERN = re.compile(r"drive\.google\.com/file/d/([^/?#]+)")

# Both scopes are required for full Google Workspace access.
# If ADC was set up without these scopes, re-authenticate once with:
#
#   gcloud auth application-default login \
#       --scopes=https://www.googleapis.com/auth/cloud-platform,\
#                https://www.googleapis.com/auth/spreadsheets.readonly,\
#                https://www.googleapis.com/auth/drive.readonly
#
# For authorized_user credentials (gcloud user ADC) the scope list is baked
# in at login time. Passing scopes= to google.auth.default() does NOT
# retroactively add scopes to an existing authorized_user token.
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_ADC_REAUTH_CMD = (
    "gcloud auth application-default login "
    "--scopes=https://www.googleapis.com/auth/cloud-platform,"
    "https://www.googleapis.com/auth/spreadsheets.readonly,"
    "https://www.googleapis.com/auth/drive.readonly"
)


def _normalise_whitespace(text: str) -> str:
    """Collapse runs of 3+ newlines to 2 and strip leading/trailing space."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _make_session() -> requests.Session:
    """Return a requests.Session authenticated with ADC credentials.

    Passes Sheets and Drive scopes so service-account ADC gets them
    automatically. For authorized_user (gcloud ADC) the scopes are baked
    into the refresh token at login time; the scopes= argument here is
    ignored by the google-auth library for that credential type.
    """
    try:
        credentials, _ = google.auth.default(
            scopes=[_SHEETS_SCOPE, _DRIVE_SCOPE, _CLOUD_SCOPE]
        )
        session = google.auth.transport.requests.AuthorizedSession(credentials)
    except Exception:
        session = requests.Session()
    session.headers.update({"User-Agent": "VibeBriefing/1.0"})
    session.verify = False
    return session


class BriefFetchError(Exception):
    pass


class BriefFetcher:
    def __init__(self, session: Optional[requests.Session] = None, timeout: int = 60):
        self.session = session or _make_session()
        self.timeout = timeout

    def fetch(self, url: str) -> str:
        url = url.strip()
        try:
            if m := _GDOC_PATTERN.search(url):
                return self._fetch_google_doc(m.group(1))
            if m := _GSHEET_PATTERN.search(url):
                return self._fetch_google_sheet(url, m.group(1))
            if m := _GDRIVE_PATTERN.search(url):
                return self._fetch_drive_file(m.group(1))
            if self._looks_like_pdf(url):
                return self._fetch_pdf(url)
            return self._fetch_generic(url)
        except BriefFetchError:
            raise
        except Exception as exc:
            raise BriefFetchError(f"Failed to fetch {url}: {exc}") from exc

    def _fetch_google_doc(self, doc_id: str) -> str:
        export_url = (
            f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        )
        resp = self.session.get(export_url, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise BriefFetchError(
                f"Access denied (HTTP {resp.status_code}) for Google Doc {doc_id}. "
                "Document must be shared publicly or with the service account. "
                f"If using gcloud ADC, re-run: {_ADC_REAUTH_CMD}"
            )
        resp.raise_for_status()
        return resp.text.strip()

    def _fetch_google_sheet(self, url: str, sheet_id: str) -> str:
        gid = None
        if "#gid=" in url:
            gid = url.split("#gid=")[1].split("&")[0]
        export_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        )
        if gid:
            export_url += f"&gid={gid}"
        resp = self.session.get(export_url, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise BriefFetchError(
                f"Access denied (HTTP {resp.status_code}) for Google Sheet {sheet_id}. "
                "Sheet must be shared publicly or with the service account. "
                f"If using gcloud ADC, re-run: {_ADC_REAUTH_CMD}"
            )
        resp.raise_for_status()
        return resp.text.strip()

    def _fetch_drive_file(self, file_id: str) -> str:
        # Try as plain text first; fall back to PDF extraction
        download_url = (
            f"https://drive.google.com/uc?export=download&id={file_id}"
        )
        resp = self.session.get(download_url, timeout=self.timeout, allow_redirects=True)
        if resp.status_code in (401, 403):
            raise BriefFetchError(
                f"Access denied (HTTP {resp.status_code}) for Drive file {file_id}. "
                "File must be shared publicly or with the service account. "
                f"If using gcloud ADC, re-run: {_ADC_REAUTH_CMD}"
            )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type:
            return self._extract_pdf_text(resp.content)
        return resp.text.strip()

    def _fetch_pdf(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return self._extract_pdf_text(resp.content)

    def _fetch_generic(self, url: str) -> str:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type:
            return self._extract_pdf_text(resp.content)
        return resp.text.strip()

    def _extract_pdf_text(self, content: bytes) -> str:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n".join(pages).strip()

    def to_flat_string(self, url: str) -> str:
        """Fetch the document at url and return a single clean string.

        Fetch strategy (three tiers, each tried in order on failure):

        Google Sheets:
          1. Sheets API v4 — fetches all tabs with proper tab labels.
             Requires spreadsheets.readonly scope in the ADC token.
          2. Authenticated CSV export — falls back when Sheets API returns
             401/403 (scope missing or API disabled). Works when the ADC
             token includes the correct scope.
          3. Unauthenticated CSV export — last resort for sheets shared as
             "Anyone with the link". Does not send an Authorization header,
             so scope issues cannot cause rejection.

        Google Docs:
          1. Authenticated txt export.
          2. Unauthenticated txt export (for publicly shared docs).

        PDFs / generic URLs: single authenticated fetch, no fallback needed.

        On any fetch error: logs the error and returns "" so the ingestion
        pipeline continues with an empty brief text rather than aborting.
        """
        url = url.strip()
        if not url:
            return ""
        try:
            if m := _GSHEET_PATTERN.search(url):
                sheet_id = m.group(1)
                # Tier 1: Sheets API v4 (all tabs, labelled)
                try:
                    return self._fetch_sheet_all_tabs(sheet_id)
                except BriefFetchError:
                    pass
                # Tier 2: authenticated CSV export
                try:
                    return _normalise_whitespace(
                        self._fetch_google_sheet(url, sheet_id)
                    )
                except BriefFetchError:
                    pass
                # Tier 3: unauthenticated CSV export (publicly shared sheets)
                text = self._fetch_url_anon(
                    self._sheet_export_url(url, sheet_id)
                )
                if text:
                    return _normalise_whitespace(text)
                _log.warning(
                    "to_flat_string: all three tiers failed for Sheet %s. "
                    "If the sheet is org-restricted, fix ADC scopes with:\n    %s",
                    sheet_id,
                    _ADC_REAUTH_CMD,
                )
                return ""

            if m := _GDOC_PATTERN.search(url):
                doc_id = m.group(1)
                # Tier 1: authenticated txt export
                try:
                    return _normalise_whitespace(self._fetch_google_doc(doc_id))
                except BriefFetchError:
                    pass
                # Tier 2: unauthenticated txt export (publicly shared docs)
                export_url = (
                    f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
                )
                text = self._fetch_url_anon(export_url)
                if text:
                    return _normalise_whitespace(text)
                _log.warning(
                    "to_flat_string: both tiers failed for Doc %s. "
                    "If the doc is org-restricted, fix ADC scopes with:\n    %s",
                    doc_id,
                    _ADC_REAUTH_CMD,
                )
                return ""

            if m := _GDRIVE_PATTERN.search(url):
                return _normalise_whitespace(self._fetch_drive_file(m.group(1)))
            if self._looks_like_pdf(url):
                return self._fetch_pdf(url)
            return _normalise_whitespace(self._fetch_generic(url))
        except Exception as exc:
            _log.warning("to_flat_string failed for %s: %s", url, exc)
            return ""

    def _sheet_export_url(self, url: str, sheet_id: str) -> str:
        """Build the CSV export URL, preserving the gid tab parameter if present."""
        gid = None
        if "#gid=" in url:
            gid = url.split("#gid=")[1].split("&")[0]
        export = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        )
        if gid:
            export += f"&gid={gid}"
        return export

    def _fetch_url_anon(self, url: str) -> str:
        """Fetch a URL without sending any Authorization header.

        Used as a last-resort fallback for documents shared as
        "Anyone with the link can view". Returns "" on any error.
        Deliberately does not use self.session so that no Bearer token
        is attached — sending a token with the wrong scope can cause
        Google to reject a request that would otherwise succeed
        anonymously.
        """
        try:
            resp = requests.get(url, timeout=self.timeout, verify=False,
                                headers={"User-Agent": "VibeBriefing/1.0"})
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception as exc:
            _log.debug("_fetch_url_anon failed for %s: %s", url, exc)
        return ""

    def _fetch_sheet_all_tabs(self, sheet_id: str) -> str:
        """Fetch every tab of a Google Sheet and join with TAB-labelled separators."""
        meta_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
            "?fields=sheets.properties"
        )
        meta_resp = self.session.get(meta_url, timeout=self.timeout)
        if meta_resp.status_code in (401, 403, 404):
            raise BriefFetchError(
                f"Cannot access Sheet metadata for {sheet_id}: "
                f"HTTP {meta_resp.status_code}"
            )
        meta_resp.raise_for_status()

        sheets_data = meta_resp.json().get("sheets", [])
        blocks: list[str] = []

        for sheet_info in sheets_data:
            props = sheet_info.get("properties", {})
            tab_name = props.get("title", "Sheet")
            gid = props.get("sheetId", 0)

            export_url = (
                f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                f"/export?format=csv&gid={gid}"
            )
            csv_resp = self.session.get(export_url, timeout=self.timeout)
            if csv_resp.status_code != 200:
                continue  # skip inaccessible tabs; do not abort

            lines = [ln for ln in csv_resp.text.splitlines() if ln.strip()]
            if not lines:
                continue
            blocks.append(f"--- TAB: {tab_name} ---\n\n" + "\n".join(lines))

        return "\n\n".join(blocks)

    @staticmethod
    def _looks_like_pdf(url: str) -> bool:
        lower = url.lower().split("?")[0]
        return lower.endswith(".pdf")
