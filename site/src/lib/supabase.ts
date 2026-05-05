import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.PUBLIC_SUPABASE_URL;
const supabaseAnonKey = import.meta.env.PUBLIC_SUPABASE_ANON_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error('Missing Supabase environment variables');
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey);

// Types match the merged adaptbase-schema.
// TODO: Replace with path import from @adaptbase/schema-ts once types are
// regenerated after the consolidation migration is applied.
export interface Document {
  id: string;
  ingest_run_id: string | null;
  source: string;
  source_id: string | null;
  title: string;
  url: string | null;
  content_type: string | null;
  raw_content: string | null;
  metadata: Record<string, any>;
  created_at: string;
  updated_at: string;

  // Plans-specific columns (added in consolidation migration)
  storage_path: string | null;
  thumbnail_path: string | null;
  page_count: number | null;
  file_size: number | null;
  sha256: string | null;
  indexed_at: string | null;
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
  indexed_documents: number;
  total_chunks: number;
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
