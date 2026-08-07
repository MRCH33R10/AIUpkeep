import requests

def check_micron_job_status(job_id: str) -> dict:
    """
    Checks whether a Micron (Workday-hosted) job posting is still open.
    job_id example: 'JR12345'
    """
    url = f"https://micron.wd1.myworkdayjobs.com/wday/cxs/micron/External/job/{job_id}"
    headers = {
        "User-Agent": "Mozilla/5.0",  # Workday sometimes blocks default requests UA
        "Accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as e:
        return {"job_id": job_id, "status": "error", "detail": str(e)}

    if response.status_code == 200:
        data = response.json()
        title = data.get("jobPostingInfo", {}).get("title", "Unknown title")
        return {"job_id": job_id, "status": "open", "title": title}
    elif response.status_code == 404:
        return {"job_id": job_id, "status": "closed_or_not_found"}
    else:
        return {"job_id": job_id, "status": "unknown", "http_code": response.status_code}


def extract_job_id_from_url(job_url: str) -> str:
    """Pulls the requisition ID (e.g. JR12345) off the end of a Workday job URL."""
    return job_url.rstrip("/").split("/")[-1].split("_")[-1]


if __name__ == "__main__":
    # Option 1: pass the job ID directly
    print(check_micron_job_status("JR12345"))

    # Option 2: pass the full URL and extract the ID
    url = "https://micron.wd1.myworkdayjobs.com/en-US/External/job/Boise-Idaho/Design-Verification-Engineer_JR12345"
    job_id = extract_job_id_from_url(url)
    print(check_micron_job_status(job_id))