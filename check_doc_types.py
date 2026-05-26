#!/usr/bin/env python3
"""
Cascade delete 5 legislation documents and convert 2 to type 'plan'.
"""

import os
from supabase import create_client
from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

url = os.getenv("PUBLIC_SUPABASE_URL")
key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
if not url or not key:
    raise ValueError("PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase = create_client(url, key)

# Documents to DELETE
DELETE_IDS = [
    "0b6ca579-9f5b-4256-992a-30b15b4749cf",  # Código Ambiental do Município de Londrina
    "00affdec-6c0f-472e-b82d-73bef0bcff7c",  # Reglamento de la Ley 21.202
    "9c2dfa84-df02-437a-9624-4185a11303bb",  # Ley de Ordenamiento Territorial Oaxaca
    "2f3e4094-17d7-461c-8331-d2e6b58ef4ac",  # Pontianak Strategic Plan
    "a0f1fbc6-8d1f-41f5-baec-155ce7cfbf75",  # Khyber Pakhtunkhwa Act
]

# Documents to KEEP (convert to "plan")
CONVERT_IDS = [
    "42858d9e-a8a8-444f-9b8a-ca18ec4351f9",  # Jakarta Regional Disaster Management Plan
    "9179004c-9597-4b22-a3f8-cc480fcec86b",  # Semarang Environmental Plan
]

print("Step 0: Check table schemas\n")

# Check entities
resp = supabase.table("entities").select("*").limit(1).execute()
if resp.data:
    print(f"Entities columns: {list(resp.data[0].keys())}")

# Check triplets
resp = supabase.table("triplets").select("*").limit(1).execute()
if resp.data:
    print(f"Triplets columns: {list(resp.data[0].keys())}")

# Check evidence
resp = supabase.table("evidence").select("*").limit(1).execute()
if resp.data:
    print(f"Evidence columns: {list(resp.data[0].keys())}")

# Check chunks
resp = supabase.table("chunks").select("*").limit(1).execute()
if resp.data:
    print(f"Chunks columns: {list(resp.data[0].keys())}")

print("\nStep 1: Find chunks from DELETE documents")
resp = supabase.table("chunks").select("id,document_id").in_("document_id", DELETE_IDS).execute()
chunk_ids = [c["id"] for c in resp.data]
print(f"Found {len(chunk_ids)} chunks from delete documents")

print("\nStep 2: Find evidence from these chunks")
if chunk_ids:
    resp = supabase.table("evidence").select("*").in_("chunk_id", chunk_ids).execute()
    print(f"Found {len(resp.data)} evidence records")

    entity_ids = list(set(e["entity_id"] for e in resp.data if e["entity_id"]))
    triplet_ids = list(set(e["triplet_id"] for e in resp.data if e["triplet_id"]))
    print(f"  - {len(entity_ids)} unique entities")
    print(f"  - {len(triplet_ids)} unique triplets")
else:
    print("No chunks to check")
    entity_ids = []
    triplet_ids = []

print("\nStep 3: Find triplets involving these entities")
if entity_ids:
    # Find triplets where these entities are subject or object
    resp_subj = supabase.table("triplets").select("id").in_("subject_id", entity_ids).execute()
    resp_obj = supabase.table("triplets").select("id").in_("object_id", entity_ids).execute()
    all_triplet_ids = list(set([t["id"] for t in resp_subj.data] + [t["id"] for t in resp_obj.data] + triplet_ids))
    print(f"Found {len(all_triplet_ids)} unique triplets total (subjects+objects+evidence)")
else:
    print("No entities found")
    all_triplet_ids = triplet_ids

print("\nReady to delete:")
print(f"  - {len(chunk_ids)} chunks")
print(f"  - {len(all_triplet_ids)} triplets")
print(f"  - {len(entity_ids)} entities")
print(f"  - 5 documents")

print("\n" + "="*60)
print("Proceeding with deletion...")
print("="*60 + "\n")

# Delete in correct order (respecting foreign keys)
print("\nDeleting triplets...")
if all_triplet_ids:
    resp = supabase.table("triplets").delete().in_("id", all_triplet_ids).execute()
    print(f"  Deleted {len(resp.data)} triplets")

print("Deleting evidence...")
if chunk_ids:
    resp = supabase.table("evidence").delete().in_("chunk_id", chunk_ids).execute()
    print(f"  Deleted {len(resp.data)} evidence records")

print("Deleting entities...")
if entity_ids:
    resp = supabase.table("entities").delete().in_("id", entity_ids).execute()
    print(f"  Deleted {len(resp.data)} entities")

print("Deleting chunks...")
if chunk_ids:
    resp = supabase.table("chunks").delete().in_("id", chunk_ids).execute()
    print(f"  Deleted {len(resp.data)} chunks")

print("Deleting documents...")
resp = supabase.table("documents").delete().in_("id", DELETE_IDS).execute()
print(f"  Deleted {len(resp.data)} documents")

print("\nConverting 2 keeper documents to type 'plan'...")
for doc_id in CONVERT_IDS:
    # Get current metadata
    resp = supabase.table("documents").select("metadata,title").eq("id", doc_id).execute()
    if resp.data:
        doc = resp.data[0]
        meta = doc.get("metadata") or {}
        meta["document_type"] = "plan"

        # Update
        resp = supabase.table("documents").update({"metadata": meta}).eq("id", doc_id).execute()
        print(f"  ✓ {doc['title'][:60]}")

print("\n✅ Done!")
