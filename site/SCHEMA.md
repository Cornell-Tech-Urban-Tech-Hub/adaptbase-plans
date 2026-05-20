# Database Schema for Plan Submissions

This document describes the Supabase tables used by the adaptbase-plans site.

**Status:** Migration completed in adaptbase-core at `supabase/migrations/20260520102324_add_plan_submissions.sql`

## `plan_submissions` Table

Stores user-submitted plans for review in the adaptbase-core admin dashboard.

### Schema

See the migration file in adaptbase-core:
`supabase/migrations/20260520102324_add_plan_submissions.sql`

**Fields:**
- `id`, `created_at` (standard)
- **Required:** `city`, `url`, `submitter_name`, `submitter_email`
- **Optional metadata:** `title`, `country`, `year`, `language`, `notes`
- **Review workflow:** `status`, `reviewed_at`, `reviewed_by`, `review_notes`

**RLS Policies:**
- Anonymous users can insert (public submission form)
- Authenticated users can read and update (admin review)

### Integration with adaptbase-core

The `plan_submissions` table should be displayed in the adaptbase-core admin dashboard's review queue, similar to the existing `feedback` table. Admins can:

1. View pending submissions
2. Mark them as approved/rejected/duplicate
3. Add review notes
4. Import approved plans into the main `documents` table

## `feedback` Table (Existing)

Already implemented for reporting problems with existing plans. See `FeedbackModal.astro` for the client-side implementation.
