import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Hacker News APIs (both public, no auth required)
ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
FIREBASE_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

# "Who is Hiring?" threads are posted monthly by user "whoishiring"
HIRING_THREAD_AUTHOR = "whoishiring"
HIRING_THREAD_TITLE_HINT = "who is hiring"

# Keywords used to identify PM roles within hiring-thread comments
PM_KEYWORDS = [
    "product manager",
    "product management",
    "senior product manager",
    "group product manager",
    "principal product manager",
    "pm role",
    " pm ",
    "fresher",
    "entry level",
    "entry-level",
    "0-1 year",
    "apm",
    "junior product manager",
    "junior pm",
    "founders office",
    "founding team",
    "growth associate",
    "growth pm",
    "ai pm",
    "product growth",
    "chief of staff",
]

# Titles containing any of these are excluded even if they mention a PM keyword
TITLE_EXCLUDE_KEYWORDS = [
    "lead",
    "senior",
    "sr.",
    "head",
    "director",
    "principal",
    "staff",
    "vp",
]

# Presence of any of these near the posting marks it as a "verified" (funded) company
FUNDING_KEYWORDS = [
    "series a",
    "series b",
    "yc",
    "y combinator",
    "funded",
    "backed",
]

# Postings matching any of these patterns (regex, case-insensitive) are rejected outright:
# explicit country-only restrictions.
LOCATION_REJECT_PATTERNS = [
    r"\bus[\s-]*only\b",
    r"\busa[\s-]*only\b",
    r"\bunited states[\s-]*only\b",
    r"\bus[\s-]*citizens?[\s-]*only\b",
    r"\bmust be (?:us|u\.s\.)[\s-]*based\b",
    r"\bcanada[\s-]*only\b",
    r"\beu[\s-]*only\b",
    r"\beurope[\s-]*only\b",
    r"\bus[\s-]*remote\b",
    r"\bremote[\s-]*\(?u\.?s\.?\)?\b",
    r"\bus[\s-]*based\b",
]

# A posting is kept only if it mentions one of these (remote/global/India-based)
LOCATION_ALLOW_KEYWORDS = [
    "remote",
    "india",
    "global",
    "worldwide",
    "anywhere",
    "distributed",
]

REQUEST_TIMEOUT_SECONDS = 15
MAX_COMMENTS_TO_SCAN = 500

DB_PATH = os.path.join(BASE_DIR, "rolereach.db")

# When set (Railway/Supabase), database.py uses PostgreSQL instead of the
# local rolereach.db SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL")

# Shared secret used to authenticate the scheduler's post-run DB sync to the
# deployed dashboard (see /api/sync-db in dashboard.py and sync step in scheduler.py).
RAILWAY_TOKEN = os.environ.get("RAILWAY_TOKEN")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://rolereach-production.up.railway.app")

# Cutshort.io public job search (no login required)
# Note: Cutshort's `page` query param does not actually paginate this feed —
# it returns the same ranked pool of jobs regardless of page number — so we scan once.
CUTSHORT_CATEGORY_URL = "https://cutshort.io/jobs/product-manager-jobs"
CUTSHORT_PAGES_TO_SCAN = 1

# Role titles we want from Cutshort; a job is kept if its headline contains any of these
CUTSHORT_ROLE_KEYWORDS = [
    "associate product manager",
    "product analyst",
    "growth pm",
    "growth product manager",
    "product manager",
    "fresher",
    "entry level",
    "entry-level",
    "junior product manager",
    "apm",
    "founders office",
    "growth associate",
    "founding team",
    "chief of staff",
]

# Countries mentioned in a Cutshort listing that disqualify it (onsite outside India, not remote)
CUTSHORT_FOREIGN_LOCATION_MARKERS = [
    "usa",
    "united states",
    "uk",
    "united kingdom",
    "canada",
    "singapore",
    "dubai",
    "uae",
    "germany",
    "australia",
    "europe",
]

# iimjobs.com job search — its listing pages return 0 embedded job data unless
# fetched through a real (or headless-rendered) browser, so requests go through
# ScraperAPI with render=true, which is sufficient to pass its bot guard.
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
SCRAPERAPI_BASE_URL = "http://api.scraperapi.com"

IIMJOBS_CATEGORY_URL = "https://www.iimjobs.com/k/it-product-management-jobs"
IIMJOBS_PAGES_TO_SCAN = 10
# Additionally scanned once, on top of the regular paginated category scan, to
# surface fresher/0-1yr roles specifically (unverified whether iimjobs actually
# honors this query param server-side — kept best-effort, ignored if it 404s
# or returns the same unfiltered set).
IIMJOBS_EXPERIENCE_FILTER_URL = "https://www.iimjobs.com/k/it-product-management-jobs?experience=0-1"

# Only these locations are kept for iimjobs listings
IIMJOBS_LOCATION_ALLOW_KEYWORDS = [
    "bangalore",
    "bengaluru",
    "remote",
]

# SerpApi Google Jobs — clean structured JSON, no bot-guard fight required
SERPAPI_URL = "https://serpapi.com/search"
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_JOBS_URL = "https://google.serper.dev/jobs"
SERPER_SEARCH_URL = "https://google.serper.dev/search"

GOOGLE_JOBS_QUERIES = [
    "Associate Product Manager Bangalore startup",
    "Product Manager Bangalore Series A",
    "Product Analyst Bangalore remote",
    "Growth Product Manager India startup",
    "Associate Product Manager fresher Bangalore",
    "APM 0-1 year Bangalore",
    "Product Manager entry level Bangalore",
    "junior product manager Bangalore",
    "APM fresher bangalore",
    "associate product manager 0 to 1 year bangalore",
    "product manager no experience bangalore startup",
    "product analyst fresher bangalore",
    "entry level product manager india remote",
    "founders office associate bangalore startup",
    "growth associate product bangalore",
    "AI product manager fresher india",
    "chief of staff associate bangalore startup",
    "growth pm fresher india remote",
    "founding team product manager bangalore",
    "product growth associate india",
]
# GOOGLE_JOBS_DATE_CHIP = "date_posted:month"  # SerpApi-specific param, unused since Serper.dev migration

# Internshala job listings — no bot protection, plain requests works
INTERNSHALA_URLS = [
    "https://internshala.com/jobs/product-management-jobs",
    "https://internshala.com/jobs/product-manager-jobs",
]

# Only these locations are kept for Google Jobs listings
GOOGLE_JOBS_LOCATION_ALLOW_KEYWORDS = [
    "bangalore",
    "bengaluru",
    "remote",
]

# Large corporations, not startups — dropped from Google Jobs results
GOOGLE_JOBS_COMPANY_EXCLUDE_KEYWORDS = [
    "sap",
    "goldman sachs",
    "capital one",
    "deloitte",
    "accenture",
    "infosys",
    "tcs",
    "wipro",
    "ibm",
    "microsoft",
    "google",
    "amazon",
    "meta",
    "mckinsey",
    "bcg",
]

# Domains to skip when resolving a company's real website — job boards,
# aggregators, and social/media platforms are never the "official website"
NON_OFFICIAL_WEBSITE_DOMAINS = [
    "linkedin.com",
    "glassdoor.",
    "indeed.",
    "shine.com",
    "naukri.com",
    "jooble.",
    "bebee.com",
    "builtin.com",
    "startup.jobs",
    "jobaaj.com",
    "monster.",
    "ziprecruiter.",
    "ambitionbox.com",
    "wellfound.com",
    "angel.co",
    "foundit.in",
    "timesjobs.com",
    "instahyre.com",
    "iimjobs.com",
    "hirist.",
    "cutshort.io",
    "simplyhired.",
    "careerbuilder.",
    "dice.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "pinterest.com",
    "tiktok.com",
    "wikipedia.org",
    "crunchbase.com",
    "medium.com",
    "google.com",
    "play.google.com",
    "apps.apple.com",
    "trustpilot.com",
    "g2.com",
    "capterra.com",
    "trustradius.com",
    "owler.com",
    "similarweb.com",
    "company-information.service.gov.uk",
    "falconebiz.com",
    "zaubacorp.com",
    "tofler.in",
    "opencorporates.com",
    "mca.gov.in",
    "internshala.com",
    "jooble.org",
    "jobrapido.com",
    "glassdoor.co.in",
    "glassdoor.com",
    "indeed.com",
    "monster.com",
]
