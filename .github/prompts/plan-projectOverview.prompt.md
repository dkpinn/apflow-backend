## High-Level Architecture

This workspace is a two-part project with a backend service in `apflow-backend` and a UI application in `apflow-frontend`.

### Backend (`apflow-backend`)

- **Main entry point**
  - `apflow-backend/app/main.py`
  - This initializes a FastAPI app with CORS and mounts API routers.

- **API layer**
  - `app/routers/`
    - `invoices.py`
    - `reconciliation.py`
    - `suppliers.py`
  - These router modules define HTTP endpoints and are included by `app/main.py`.

- **Database / persistence**
  - `app/db/supabase_client.py`
  - `db/` contains `.sql` files and likely SQL assets used by the backend.
  - The backend appears to use Supabase as its data store via the Supabase client.

- **Data models / schemas**
  - `app/models/schemas.py`
  - Defines request/response or domain types used by routers and services.

- **Business logic / services**
  - `app/services/`
    - `audit_log.py`
    - `document_jobs.py`
    - `invoice_line_items.py`
    - `invoice_ocr_pipeline.py`
    - `invoice_parse_attempts.py`
    - `invoice_previews.py`
    - `reconciliation_engine.py`
    - `supplier_matcher.py`
  - These modules encapsulate the core processing and domain operations.

- **Invoice extraction subsystem**
  - `app/services/invoice_extraction/`
  - Contains specialized parsers and helpers such as:
    - `banking_parser.py`
    - `contact_parser.py`
    - `entity_detection.py`
    - `invoice_number_parser.py`
    - `layout_analyser.py`
    - `image_quality.py`
    - and more
  - This is the OCR/processing pipeline used to parse invoices and extract structured data.

- **Tests**
  - `apflow-backend/tests/`
  - `test_receipt_parsing.py` is one example of backend test coverage.

### Frontend (`apflow-frontend`)

- **Main entry points**
  - `apflow-frontend/src/router.tsx`
    - Builds the app router using TanStack Router and a generated route tree.
  - `apflow-frontend/src/routes/__root.tsx`
    - Likely defines the top-level route layout and app wrapper.
  - `apflow-frontend/src/routes/_app.*.tsx`
    - Route-specific layout files for different application sections.

- **Routing and page structure**
  - `src/routes/`
    - Routes are organized by feature:
      - `auth.callback.tsx`
      - `login.tsx`
      - `logout.tsx`
      - `_app.admin.tsx`
      - `_app.dashboard.tsx`
      - `_app.invoices.tsx`
      - `_app.reconciliation.tsx`
      - `_app.suppliers.tsx`
      - `_app.statements.tsx`
      - and more
    - Nested routes use file-based naming for dynamic pages like `_app.invoices.$invoiceId.tsx`.

- **Generated router data**
  - `src/routeTree.gen.ts`
  - Contains the route tree structure used by `src/router.tsx`.

- **UI and component libraries**
  - `src/components/`
  - `src/lib/`
    - Contains shared logic for auth, date formatting, ingestion, org management, permissions, theme, and utilities.
  - `src/integrations/supabase/`
    - Holds Supabase integration logic for the frontend.

- **Tech stack**
  - `package.json` shows:
    - React 19
    - Vite
    - Tailwind CSS
    - TanStack Router / Query
    - Supabase JS
    - Zod, React Hook Form, Radix UI, Recharts, etc.

### How the pieces interact

- The backend exposes API routes via FastAPI, and the frontend consumes them.
- `app/main.py` is the backend runtime entry and wires routers to the app.
- Routers delegate requests to service modules, which perform business operations and use the Supabase client to read/write data.
- Invoice/OCR-specific work is centralized in `app/services/invoice_extraction/`.
- The frontend app is organized as a route-driven React SPA:
  - `src/router.tsx` creates the router
  - route files in `src/routes/` define pages and layouts
  - shared utilities and Supabase integration are in `src/lib/` and `src/integrations/supabase/`
- The two sides are separated cleanly:
  - backend handles API, data, and invoice processing
  - frontend handles user interaction, routing, and presentation

### Useful starting points

- Backend: `apflow-backend/app/main.py`
- Frontend: `apflow-frontend/src/router.tsx`
- Frontend routes: `apflow-frontend/src/routes/`
- Backend services: `apflow-backend/app/services/`
- Frontend integrations: `apflow-frontend/src/integrations/supabase/`

If you want, I can also map a specific feature flow like “invoice upload through the frontend to backend processing.”