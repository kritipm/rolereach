import database
import fix_company_names
import scraper


def main():
    database.init_db()
    thread_title, thread_id, matches = scraper.scrape_pm_jobs()

    print(f"Thread: {thread_title}")
    print(f"Thread ID: {thread_id}")
    print(f"PM postings found: {len(matches)}\n")

    for job in matches:
        preview = job["text"][:300].replace("\n", " ")
        verified_label = "Yes" if job["verified"] else "No"
        print(f"- [{job['author']}] {job['url']}")
        print(f"  company_url: {job['company_url'] or 'N/A'}")
        print(f"  verified: {verified_label}")
        print(f"  {preview}...\n")

    changes, unresolved = fix_company_names.fix_names()
    if changes:
        print(f"fix_company_names: corrected {len(changes)} name(s):")
        for old_name, new_name in changes:
            print(f"  {old_name!r} -> {new_name!r}")


if __name__ == "__main__":
    main()
