# powerbuilder/chat/utils/cook_client.py
"""
CookPoliticalClient — Cook Political Report API integration.

Architecture
------------
1. Credentials: COOK_EMAIL and COOK_PASSWORD must be set. Methods return
   _null_result() gracefully when credentials are absent so the pipeline
   continues without Cook data.
2. Static seed file first: `data/cook_pvi_2025.json` is checked before any
   network call for the legacy fetch() path.
3. 24-hour local cache: list-endpoint responses are written to
   `data/cook_cache/list_{endpoint}.json`. A fresh cache hit skips the API.
4. Live API: Base URL https://cookpolitical.com/api/race/ with endpoints
   house / senate / governor / presidential. HTTP Basic Auth with base64-
   encoded credentials; responds with JSON arrays.

Returned dict schema (from get_district_rating / get_senate_rating / etc.)
---------------------------------------------------------------------------
{
    "cook_pvi":    "R+5" | "D+3" | "EVEN" | None,
    "race_rating": "Lean R" | "Likely D" | "Toss-up" | None,
    "incumbent":   "Rep. Jane Doe (R)" | None,
    "cycle":       2026 | None,
    "source":      "seed" | "cache" | "api" | None,
}

None values mean Cook data was not available (no credentials, API error,
or district not covered).
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../data/cook_cache"))
SEED_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../data/cook_pvi_2025.json"))
CACHE_TTL = timedelta(hours=24)

BASE_URL = "https://cookpolitical.com/api/race/"

# Mapping from Cook's raw rating strings to normalised labels
RATING_MAP = {
    "safe democrat":     "Safe D",
    "likely democrat":   "Likely D",
    "lean democrat":     "Lean D",
    "toss-up":           "Toss-up",
    "toss up":           "Toss-up",
    "lean republican":   "Lean R",
    "likely republican": "Likely R",
    "safe republican":   "Safe R",
}

# ---------------------------------------------------------------------------
# Null result helper
# ---------------------------------------------------------------------------

def _null_result(source: Optional[str] = None) -> dict:
    return {
        "cook_pvi":    None,
        "race_rating": None,
        "incumbent":   None,
        "cycle":       None,
        "source":      source,
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CookPoliticalClient:
    """
    Fetches Cook Political Report ratings via the live API with a 24-hour
    local disk cache. Falls back to _null_result() when credentials are absent
    so callers never have to handle exceptions.

    Endpoints (all under BASE_URL):
        house         → list of {Title, State, District, Incumbent, Cook_PVI, Rating, Cycle, Rating_date}
        senate        → list of {Title, State, Incumbent, Rating}
        governor      → list of {Title, State, Incumbent, Rating}
        presidential  → list of {Title, State, Incumbent, Rating}
    """

    def __init__(self):
        self.email    = os.environ.get("COOK_EMAIL", "").strip()
        self.password = os.environ.get("COOK_PASSWORD", "").strip()
        self._has_creds = bool(self.email and self.password)

        if self._has_creds:
            raw = f"{self.email}:{self.password}".encode()
            self._auth_header = "Basic " + base64.b64encode(raw).decode()
        else:
            self._auth_header = None
            logger.info(
                "Cook Political Report credentials not configured — ratings unavailable. "
                "Set COOK_EMAIL and COOK_PASSWORD to enable."
            )

        self._seed: dict = self._load_seed()

    # ------------------------------------------------------------------
    # High-level list fetchers
    # ------------------------------------------------------------------

    def get_house_ratings(self) -> Optional[list]:
        """Return all house race records from the API (cached 24 h)."""
        return self._fetch_endpoint_list("house")

    def get_senate_ratings(self) -> Optional[list]:
        """Return all senate race records from the API (cached 24 h)."""
        return self._fetch_endpoint_list("senate")

    def get_governor_ratings(self) -> Optional[list]:
        """Return all governor race records from the API (cached 24 h)."""
        return self._fetch_endpoint_list("governor")

    def get_presidential_ratings(self) -> Optional[list]:
        """Return all presidential race records from the API (cached 24 h)."""
        return self._fetch_endpoint_list("presidential")

    # ------------------------------------------------------------------
    # District-level lookups (normalized return schema)
    # ------------------------------------------------------------------

    def get_district_rating(self, state: str, district_num: int) -> dict:
        """
        Return the Cook rating for a specific U.S. House district.

        Parameters
        ----------
        state        : 2-char abbreviation or full state name
        district_num : integer district number (1-based; 1 for at-large)

        Returns a dict matching the module-level schema, or _null_result()
        if the district is not found or credentials are absent.
        """
        state_abbr = self._normalize_state_abbr(state)
        dist_str   = str(int(district_num))

        ratings = self.get_house_ratings()
        if not ratings:
            return _null_result()

        dist_norm = dist_str.lstrip("0") or "0"
        for record in ratings:
            rec_state     = str(record.get("State", "")).strip().upper()
            rec_dist_norm = str(record.get("District", "")).strip().lstrip("0") or "0"
            if rec_state == state_abbr.upper() and rec_dist_norm == dist_norm:
                return self._normalise_record(record, "api")

        return _null_result()

    def get_senate_rating(self, state: str) -> dict:
        """
        Return the Cook rating for the U.S. Senate race in a given state.

        Parameters
        ----------
        state : 2-char abbreviation or full state name

        Returns a dict matching the module-level schema, or _null_result().
        """
        state_abbr = self._normalize_state_abbr(state)
        ratings    = self.get_senate_ratings()
        if not ratings:
            return _null_result()

        for record in ratings:
            rec_state = str(record.get("State", "")).strip().upper()
            if rec_state == state_abbr.upper():
                return self._normalise_record(record, "api")

        return _null_result()

    def get_governor_rating(self, state: str) -> dict:
        """
        Return the Cook rating for the Governor race in a given state.

        Parameters
        ----------
        state : 2-char abbreviation or full state name

        Returns a dict matching the module-level schema, or _null_result().
        """
        state_abbr = self._normalize_state_abbr(state)
        ratings    = self.get_governor_ratings()
        if not ratings:
            return _null_result()

        for record in ratings:
            rec_state = str(record.get("State", "")).strip().upper()
            if rec_state == state_abbr.upper():
                return self._normalise_record(record, "api")

        return _null_result()

    # ------------------------------------------------------------------
    # Legacy fetch() — kept for backward compatibility
    # ------------------------------------------------------------------

    def fetch(
        self,
        district_type: str,
        district_id: str,
        state_fips: str,
        cycle: int = 2026,
    ) -> dict:
        """
        Legacy entry point. Prefer get_district_rating() / get_senate_rating()
        / get_governor_rating() for new callers.
        """
        # Districts Cook doesn't cover
        if district_type in ("state_house", "state_senate"):
            return _null_result()

        # 1. Static seed (no credentials required)
        seed_result = self._check_seed(district_type, district_id, state_fips)
        if seed_result:
            return {**seed_result, "source": "seed"}

        if not self._has_creds:
            return _null_result()

        state_abbr = self._fips_to_abbr(state_fips)

        if district_type == "senate":
            return self.get_senate_rating(state_abbr)
        if district_type == "governor":
            return self.get_governor_rating(state_abbr)

        # congressional
        try:
            dist_num = int(district_id[len(state_fips):].lstrip("0") or "1")
        except (ValueError, IndexError):
            dist_num = 1
        return self.get_district_rating(state_abbr, dist_num)

    # ------------------------------------------------------------------
    # Internal: endpoint list fetcher + cache
    # ------------------------------------------------------------------

    def _fetch_endpoint_list(self, endpoint: str) -> Optional[list]:
        """Fetch a full endpoint list, using the 24-hour disk cache."""
        cache_path = os.path.join(CACHE_DIR, f"list_{endpoint}.json")
        cached = self._load_list_cache(cache_path)
        if cached is not None:
            return cached

        if not self._has_creds:
            return None

        try:
            import requests
        except ImportError:
            logger.error("CookClient: 'requests' package not installed.")
            return None

        try:
            time.sleep(0.5)
            resp = requests.get(
                BASE_URL + endpoint,
                headers={
                    "Authorization": self._auth_header,
                    "Accept":        "application/json",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                self._save_list_cache(cache_path, data)
                return data
            logger.warning(f"CookClient: /{endpoint} returned non-list response")
            return None
        except Exception as e:
            logger.warning(f"CookClient: /{endpoint} request failed — {e}")
            return None

    def _load_list_cache(self, path: str) -> Optional[list]:
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            cached_at = datetime.fromisoformat(data.get("_cached_at", "1970-01-01"))
            if datetime.now() - cached_at < CACHE_TTL:
                return data.get("_items")
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            pass
        return None

    def _save_list_cache(self, path: str, items: list):
        os.makedirs(CACHE_DIR, exist_ok=True)
        payload = {"_items": items, "_cached_at": datetime.now().isoformat()}
        try:
            with open(path, "w") as f:
                json.dump(payload, f)
        except OSError as e:
            logger.warning(f"CookClient: could not write list cache to {path} — {e}")

    # ------------------------------------------------------------------
    # Internal: dict-based cache (legacy fetch() path)
    # ------------------------------------------------------------------

    def _cache_key(self, district_type: str, district_id: str, state_fips: str, cycle: int) -> str:
        safe_id = district_id.replace("/", "_")
        return f"{state_fips}_{district_type}_{safe_id}_{cycle}"

    def _load_cache(self, path: str) -> Optional[dict]:
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            cached_at = datetime.fromisoformat(data.get("_cached_at", "1970-01-01"))
            if datetime.now() - cached_at < CACHE_TTL:
                data.pop("_cached_at", None)
                return data
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
        return None

    def _save_cache(self, path: str, data: dict):
        os.makedirs(CACHE_DIR, exist_ok=True)
        payload = {**data, "_cached_at": datetime.now().isoformat()}
        try:
            with open(path, "w") as f:
                json.dump(payload, f)
        except OSError as e:
            logger.warning(f"CookClient: could not write cache to {path} — {e}")

    # ------------------------------------------------------------------
    # Static seed
    # ------------------------------------------------------------------

    def _load_seed(self) -> dict:
        try:
            with open(SEED_PATH) as f:
                data = json.load(f)
            logger.debug(f"CookClient: loaded seed file ({len(data.get('districts', {}))} districts)")
            return data
        except FileNotFoundError:
            logger.debug("CookClient: seed file not found — PVI seed unavailable.")
            return {}
        except json.JSONDecodeError as e:
            logger.warning(f"CookClient: seed file parse error — {e}")
            return {}

    def _check_seed(self, district_type: str, district_id: str, state_fips: str) -> Optional[dict]:
        districts = self._seed.get("districts", {})
        if district_type == "senate":
            key = f"{state_fips}_senate"
        elif district_type == "governor":
            key = f"{state_fips}_gov"
        else:
            key = district_id
        entry = districts.get(key)
        if not entry:
            return None
        return {
            "cook_pvi":    entry.get("cook_pvi"),
            "race_rating": entry.get("race_rating"),
            "incumbent":   entry.get("incumbent"),
            "cycle":       entry.get("cycle"),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_record(record: dict, source: str) -> dict:
        raw_rating = str(record.get("Rating", "")).lower().strip()
        return {
            "cook_pvi":    record.get("Cook_PVI") or record.get("cook_pvi"),
            "race_rating": RATING_MAP.get(raw_rating) or record.get("Rating") or None,
            "incumbent":   record.get("Incumbent") or record.get("incumbent"),
            "cycle":       record.get("Cycle") or record.get("cycle") or 2026,
            "source":      source,
        }

    @staticmethod
    def _normalize_state_abbr(state: str) -> str:
        """Convert full state name or 2-char abbreviation to uppercase 2-char abbreviation."""
        state = state.strip()
        if len(state) == 2:
            return state.upper()
        # Full name → FIPS → abbr (avoids circular import by using inline dict)
        _NAME_TO_FIPS = {
            "alabama": "01", "alaska": "02", "arizona": "04", "arkansas": "05",
            "california": "06", "colorado": "08", "connecticut": "09", "delaware": "10",
            "district of columbia": "11", "florida": "12", "georgia": "13", "hawaii": "15",
            "idaho": "16", "illinois": "17", "indiana": "18", "iowa": "19", "kansas": "20",
            "kentucky": "21", "louisiana": "22", "maine": "23", "maryland": "24",
            "massachusetts": "25", "michigan": "26", "minnesota": "27", "mississippi": "28",
            "missouri": "29", "montana": "30", "nebraska": "31", "nevada": "32",
            "new hampshire": "33", "new jersey": "34", "new mexico": "35", "new york": "36",
            "north carolina": "37", "north dakota": "38", "ohio": "39", "oklahoma": "40",
            "oregon": "41", "pennsylvania": "42", "rhode island": "44", "south carolina": "45",
            "south dakota": "46", "tennessee": "47", "texas": "48", "utah": "49",
            "vermont": "50", "virginia": "51", "washington": "53", "west virginia": "54",
            "wisconsin": "55", "wyoming": "56",
        }
        fips = _NAME_TO_FIPS.get(state.lower())
        if fips:
            return CookPoliticalClient._FIPS_TO_ABBR.get(fips, state[:2].upper())
        return state[:2].upper()

    # Inverted from GeographyStandardizer.STATE_FIPS — only 2-char abbreviations.
    _FIPS_TO_ABBR = {
        "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
        "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
        "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
        "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
        "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
        "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
        "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
        "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
        "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
        "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
        "56": "WY",
    }

    def _fips_to_abbr(self, fips: str) -> str:
        return self._FIPS_TO_ABBR.get(fips.zfill(2), fips)
