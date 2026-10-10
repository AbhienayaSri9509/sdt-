# Employee Attendance & Analytics API

A FastAPI and MongoDB service providing punch-in/punch-out attendance tracking with business rules (R1–R10), manual regularization audit trail, and high-performance analytics aggregation pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Atlas or local instance)

## Local Setup & Running

1. **Clone the repository and set up a virtual environment:**
   ```bash
   git clone <repo-url>
   cd <repo-folder>
   python -m venv .venv
   # Linux/macOS:
   source .venv/bin/activate
   # Windows:
   .venv\Scripts\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   Set `MONGO_URI` and `MONGO_DB` in your environment or in a `.env` file (real environment variables take precedence):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

4. **Seed Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

5. **Start the Service:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive API documentation is accessible at `http://localhost:8000/docs`.

## Project Structure
- `app/main.py`: Complete application code containing endpoints, business rules, helpers, and MongoDB aggregation pipelines.
- `requirements.txt`: Python dependencies.
- `REVIEW.md`: Comprehensive review of starter code defects, symptoms, and fixes.
- `DECISIONS.md`: Design rationale for database indexes, concurrency handling, tie-breaking, headcount calculation, and 100x scalability.
- `DATA_MODEL.md` & `openapi.yaml`: Stored data model specification and OpenAPI contract.

## Implementation Details
- **Idempotent Indexes at Startup**: Indexes are created automatically during the application lifespan (`app/main.py:lifespan`).
- **Race-Safe Concurrency**: Unique compound indexes on `(emp_code, date)` and atomic operations ensure that concurrent punch-in/out and regularization operations do not conflict or lose audit history.
- **Rules R1–R10 Compliant**: Supports overnight shifts across midnight, 10-minute grace period with full duration calculation, 30-minute overtime threshold, half-day detection, Mon-Fri working days, and exact half-up rounding.
- **MongoDB Aggregation Pipelines**: Multi-collection aggregation for department summary with zero-log headcount preservation, competition ranking via `$setWindowFields` (`$rank`), gap-filling date densification via `$densify`, and query plan explainability (`/admin/explain/{endpoint}`).
