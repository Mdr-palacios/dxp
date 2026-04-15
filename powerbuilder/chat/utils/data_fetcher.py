# powerbuilder/chat/utils/data_fetcher.py
import os
import requests
from dotenv import load_dotenv
from .census_vars import (
    VOTER_DEMOGRAPHICS,
    RACE_TABLES,
    SEX_AGE_OFFSETS,
)

load_dotenv()

CENSUS_KEY = os.getenv("CENSUS_API_KEY")
FEC_KEY = os.getenv("FEC_API_KEY")

class DataFetcher:
    
    @staticmethod
    def search_census_variables(keyword, year=2022, dataset="acs/acs5"):
        """
        Discovery Tool: Searches the Census metadata for variables matching a keyword.
        """
        url = f"https://api.census.gov/data/{year}/{dataset}/variables.json"
        try:
            response = requests.get(url)
            if response.status_code == 200:
                variables = response.json().get("variables", {})
                # Filter variables where keyword appears in the label or concept
                matches = {
                    k: v['label'] for k, v in variables.items() 
                    if keyword.lower() in v.get('label', '').lower() 
                    or keyword.lower() in v.get('concept', '').lower()
                }
                return matches
            return {"error": f"Failed to reach discovery tool: {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def get_census_data(state_fips, variables=["total_pop"], geo_level="county"):
        """
        Dynamic Fetcher with Geography Toggle.
        Supports: 'statewide', 'county', 'congressional', 'state_senate', 'state_house', 'precinct'
        """
        # 1. GEOGRAPHY TOGGLE: Friendly names to Census API predicates
        GEO_MAP = {
            "statewide": {"for": "state:*", "in": ""},
            "county": {"for": "county:*", "in": f"state:{state_fips}"},
            "congressional": {"for": "congressional district:*", "in": f"state:{state_fips}"},
            "state_senate": {"for": "state legislative district (upper chamber):*", "in": f"state:{state_fips}"},
            "state_house": {"for": "state legislative district (lower chamber):*", "in": f"state:{state_fips}"},
            "precinct": {"for": "block group:*", "in": f"state:{state_fips} county:*"}
        }

        geo_config = GEO_MAP.get(geo_level, GEO_MAP["county"])

        # 2. TRANSLATION: Human keys -> Census codes
        census_codes = [VOTER_DEMOGRAPHICS.get(v, v) for v in variables]
        get_vars = "NAME," + ",".join(census_codes)
        
        url = f"https://api.census.gov/data/2022/acs/acs5"
        params = {
            "get": get_vars,
            "for": geo_config["for"],
            "key": os.getenv("CENSUS_API_KEY")
        }
        
        if geo_config["in"]:
            params["in"] = geo_config["in"]

        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            headers = data[0]
            return [dict(zip(headers, row)) for row in data[1:]]
        except Exception as e:
            return [{"error": f"Census API failure: {str(e)}"}]
    
    @staticmethod
    def get_computed_census_data(state_fips, race, gender, age_min, age_max, geo_level="precinct"):
        """
        Dynamically constructs and sums Census variables for specific intersections.
        """
        table_prefix = RACE_TABLES.get(race, "B01001") # Default to total pop
        
        # Logic to determine which row offsets to pull based on age_min/age_max
        required_offsets = DataFetcher._resolve_age_offsets(gender, age_min, age_max)
        
        # Construct codes: e.g., "B01001I_012E", "B01001I_013E"
        census_codes = [f"{table_prefix}_{offset}E" for offset in required_offsets]
        
        # Fetch raw data for all codes
        raw_results = DataFetcher.get_census_data(state_fips, variables=census_codes, geo_level=geo_level)
        
        # SUMMING LOGIC: Create a new key 'target_population' by adding the codes
        for row in raw_results:
            row['target_population'] = sum(float(row.get(code, 0)) for code in census_codes)
            
        return raw_results
    
    @staticmethod
    def _resolve_age_offsets(gender, age_min, age_max):
        """
        Helper to find all row offsets that fit within an age range.
        Ex: 30 to 50 would return ['012', '013', '014'] for Male.
        """
        # Mapping ranges to their descriptive keys in SEX_AGE_OFFSETS
        range_map = [
            (0, 4, "under_5"), (5, 9, "5_9"), (10, 14, "10_14"), 
            (15, 17, "15_17"), (18, 19, "18_19"), (20, 20, "20"), 
            (21, 21, "21"), (22, 24, "22_24"), (25, 29, "25_29"), 
            (30, 34, "30_34"), (35, 44, "35_44"), (45, 54, "45_54"), 
            (55, 64, "55_64"), (65, 74, "65_74"), (75, 84, "75_84"), (85, 200, "85_plus")
        ]
        
        selected_offsets = []
        for low, high, key in range_map:
            # If the bracket overlaps with the user's range, we grab the offset
            if not (high < age_min or low > age_max):
                offset = SEX_AGE_OFFSETS[gender].get(key)
                if offset:
                    selected_offsets.append(offset)
        return selected_offsets

    @staticmethod
    def get_custom_crosstab(state_fips, race="total", gender="female", age_min=18, age_max=99, geo_level="precinct"):
        """
        The Master Query Node: Builds any Race x Gender x Age combination.
        """
        table = RACE_TABLES.get(race, "B01001")
        offsets = DataFetcher._resolve_age_offsets(gender, age_min, age_max)
        
        # Build the codes: e.g., "B01001B_022E"
        codes = [f"{table}_{o}E" for o in offsets]
        
        # Fetch raw data
        raw_data = DataFetcher.get_census_data(state_fips, variables=codes, geo_level=geo_level)
        
        # Sum the results for the LLM
        for row in raw_data:
            row['target_pop'] = sum(float(row.get(c, 0)) for c in codes if row.get(c))
            
        return raw_data

    ############## 2020 DECENNIAL CENSUS -- BLOCK GROUP LEVEL ##############

    @staticmethod
    def get_decennial_vap_by_block_group(state_fips: str) -> dict:
        """
        Fetches Voting Age Population (18+) at block group level from the
        2020 Decennial Census PL 94-171 redistricting file.

        Variable P0030001 = Total population 18 years and over.

        Returns:
            dict mapping 12-character bg_geoid → vap (int)
            e.g. {"440050101011": 1823}

        Falls back to ACS5 total_population (B01003_001E) if the Decennial
        API call fails. The fallback is a less precise proxy (all ages, not 18+)
        but avoids a hard failure in the pipeline.
        """
        # 2020 Decennial PL94-171: P3_001N = total population 18+ (VAP)
        # Note: 2020 Decennial uses P3_001N; 2010 used P003001 (different naming convention)
        # Response returns component columns (state, county, tract, block group), not GEO_ID
        url = "https://api.census.gov/data/2020/dec/pl"
        params = {
            "get": "P3_001N",
            "for": "block group:*",
            "in": f"state:{state_fips} county:* tract:*",
            "key": os.getenv("CENSUS_API_KEY"),
        }
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            headers = data[0]
            result = {}
            for row in data[1:]:
                r = dict(zip(headers, row))
                # Reconstruct 12-char GEOID: state(2) + county(3) + tract(6) + bg(1)
                bg_geoid = (
                    r.get("state", "").zfill(2)
                    + r.get("county", "").zfill(3)
                    + r.get("tract", "").zfill(6)
                    + r.get("block group", "")
                )
                if bg_geoid:
                    result[bg_geoid] = int(r.get("P3_001N", 0) or 0)
            return result
        except Exception as e:
            print(f"  Warning: Decennial VAP fetch failed ({e}); falling back to ACS5 total_population.")
            rows = DataFetcher.get_census_data(state_fips, ["total_population"], geo_level="precinct")
            result = {}
            for row in rows:
                if "error" in row:
                    continue
                bg_geoid = (
                    row.get("state", "").zfill(2)
                    + row.get("county", "").zfill(3)
                    + row.get("tract", "").zfill(6)
                    + row.get("block group", "")
                )
                result[bg_geoid] = int(float(row.get("B01003_001E", 0) or 0))
            return result

    ############## CENSUS TIGER/LINE SPATIAL BOUNDARIES ##############

    @staticmethod
    def get_congressional_district_boundary(state_fips: str, district_id: str):
        """
        Downloads the 118th Congress congressional district boundary from Census
        TIGER/Line and returns a GeoDataFrame for the target district in EPSG:4326.

        The national CD shapefile (~2 MB) is cached locally at
        data/tiger_cache/tl_2022_us_cd118.gpkg to avoid repeated downloads.

        Args:
            state_fips:  2-digit FIPS string, e.g. "44" for Rhode Island
            district_id: 4-char GEOID (state_fips + zero-padded district),
                         e.g. "4401" for RI-01

        Returns:
            GeoDataFrame with columns [GEOID, geometry] in EPSG:4326,
            or None if the file cannot be downloaded or the district is not found.
        """
        try:
            import geopandas as gpd
        except ImportError:
            print("  geopandas is required for get_congressional_district_boundary.")
            return None

        TIGER_CD_URL = (
            "https://www2.census.gov/geo/tiger/TIGER2022/CD/tl_2022_us_cd116.zip"
        )
        cache_dir  = "data/tiger_cache"
        cache_path = os.path.join(cache_dir, "tl_2022_us_cd116.gpkg")

        try:
            os.makedirs(cache_dir, exist_ok=True)

            if os.path.exists(cache_path):
                all_cds = gpd.read_file(cache_path)
            else:
                print("  Downloading congressional district boundaries (116th Congress)...")
                all_cds = gpd.read_file(TIGER_CD_URL)
                all_cds.to_file(cache_path, driver="GPKG")

            # GEOID in the TIGER file is state_fips(2) + district(2), e.g. "4401"
            district_gdf = all_cds[all_cds["GEOID"] == district_id].copy()
            if district_gdf.empty:
                state_ids = sorted(
                    all_cds[all_cds["STATEFP"] == state_fips]["GEOID"].tolist()
                )
                print(
                    f"  Warning: District GEOID '{district_id}' not found in TIGER CD file. "
                    f"GEOIDs for state {state_fips}: {state_ids}"
                )
                return None

            # Ensure WGS84 CRS (TIGER files are NAD83/EPSG:4269; reproject to 4326)
            if district_gdf.crs is None:
                district_gdf = district_gdf.set_crs("EPSG:4326")
            else:
                district_gdf = district_gdf.to_crs("EPSG:4326")

            return district_gdf[["GEOID", "geometry"]].reset_index(drop=True)

        except Exception as e:
            print(f"  Warning: Could not load CD boundary for {district_id}: {e}")
            return None

    ############## FEC / CAMPAIGN FINANCE DATA INGESTION -- FEDERAL ONLY ##############
    @staticmethod
    def get_district_finances(state, district_number, office_type, cycle=2024):
        """
        Fetches spending and receipts for a specific race.
        Office types: 'H' (House), 'S' (Senate), 'P' (Presidential)
        """
        api_key = os.getenv("FEC_API_KEY")
        base_url = "https://api.open.fec.gov/v1/candidates/totals/"
        
        params = {
            "api_key": api_key,
            "cycle": cycle,
            "state": state,
            "district": district_number,
            "office": office_type,
            "sort": "-receipts"
        }
        
        response = requests.get(base_url, params=params)
        if response.status_code == 200:
            raw_data = response.json().get("results", [])
            # NORMALIZATION: Simplify for the LLM
            normalized = []
            for candidate in raw_data:
                normalized.append({
                    "name": candidate.get("name"),
                    "party": candidate.get("party_full"),
                    "total_receipts": f"${float(candidate.get('receipts') or 0):,.2f}",
                    "total_disbursements": f"${float(candidate.get('disbursements') or 0):,.2f}",
                    "cash_on_hand": f"${float(candidate.get('cash_on_hand_end_period') or 0):,.2f}"
                })
            return normalized
        return {"error": "FEC API unreachable"}



# ADD IN OPPO RESEARCH API FROM 21ST CENTURY BRIDGE
# ADD IN ELECTION RESULTS API KEY


