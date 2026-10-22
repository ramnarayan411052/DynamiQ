# DynamiQ dashboard

The React dashboard uses the FastAPI backend for tasks, archived tasks, resources, schedules, and solver requests. The backend persists dashboard data in SQLite; the browser keeps only transient UI state.

On **Task Scheduling**, choose **Compare all algorithms** to generate side-by-side Gantt charts for CP-SAT, SJF, FCFS, and Priority using the same active jobs and resources. The comparison rebuilds each schedule without preserving existing assignments and does not replace the saved schedule. CP-SAT uses the selected objective and priority rule; unscheduled-task penalties are disabled for the comparison because the dispatching algorithms do not support them.

## Local development

Install Node.js and the backend requirements, then start the API from `backend`:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn api:app --reload --host 127.0.0.1 --port 8000
```

From this directory, run:

```powershell
npm ci
npm run dev
```

Vite proxies `/api` requests to the local backend.

## Production

Use the repository-level `docker-compose.yml` to build the production React/Nginx image and FastAPI image. The Nginx container serves the static dashboard and proxies `/api` to the backend.
