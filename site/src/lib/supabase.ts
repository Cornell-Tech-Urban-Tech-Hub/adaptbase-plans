import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.PUBLIC_SUPABASE_URL;
const supabaseAnonKey = import.meta.env.PUBLIC_SUPABASE_ANON_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error('Missing Supabase environment variables');
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey);

/* The document columns this site reads. Named, never `*`: since adaptbase-core
 * migration 20261028170000 the anon key may select only the listing columns of
 * `documents` (not raw_content, full_text or tsv, the whole text of every
 * plan), and a `*` select is refused outright. Add a column here only if it is
 * in that migration's grant. */
export const DOC_COLUMNS =
  'id, source, title, url, content_type, metadata, created_at, updated_at, storage_path, thumbnail_path, page_count, file_size, indexed_at, doc_type';

// Types match the merged adaptbase-schema.
// TODO: Replace with path import from @adaptbase/schema-ts once types are
// regenerated after the consolidation migration is applied.
export interface Document {
  id: string;
  source: string;
  title: string;
  url: string | null;
  content_type: string | null;
  metadata: Record<string, any>;
  created_at: string;
  updated_at: string;

  // Plans-specific columns (added in consolidation migration)
  storage_path: string | null;
  thumbnail_path: string | null;
  page_count: number | null;
  file_size: number | null;
  indexed_at: string | null;
  doc_type: string | null;
}

export interface Chunk {
  id: string;
  document_id: string;
  ingest_run_id: string | null;
  content: string;
  chunk_index: number;
  embedding: number[] | null;
  metadata: Record<string, any>;

  // Plans-specific column
  page_number: number | null;
}

export interface SearchResult {
  chunk_id: string;
  document_id: string;
  page_number: number | null;
  content: string;
  score: number;
  vector_rank: number | null;
  keyword_rank: number | null;
}

export interface DocumentStats {
  total_documents: number;
  total_countries: number;
  total_cities: number;
}

export function getPublicUrl(bucket: string, path: string): string {
  const { data } = supabase.storage.from(bucket).getPublicUrl(path);
  return data.publicUrl;
}

export function formatFileSize(bytes: number): string {
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = bytes;
  let unitIndex = 0;

  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex++;
  }

  return `${size.toFixed(1)} ${units[unitIndex]}`;
}
