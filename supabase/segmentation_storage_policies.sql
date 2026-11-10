-- Run this in the Supabase SQL editor. NeuroQuant uses Firestore for metadata
-- and folder-password hashes, but does not use Firebase Authentication.
-- A Supabase Edge Function with the service-role secret mints scoped signed
-- URLs after checking the folder password. Keep the bucket private.

UPDATE storage.buckets SET public = false WHERE id = 'neuro';

-- Remove the broad public policies visible in the current Supabase project.
DROP POLICY IF EXISTS "Give anon users access to JPG images in folder 1qccji_0" ON storage.objects;
DROP POLICY IF EXISTS "Give anon users access to JPG images in folder 1qccji_1" ON storage.objects;
DROP POLICY IF EXISTS "Give anon users access to JPG images in folder 1qccji_2" ON storage.objects;
DROP POLICY IF EXISTS "Give anon users access to JPG images in folder 1qccji_3" ON storage.objects;
DROP POLICY IF EXISTS "Radiologist local upload delete neuro" ON storage.objects;
DROP POLICY IF EXISTS "Radiologist local upload list neuro" ON storage.objects;
DROP POLICY IF EXISTS "anon can read neuro segmentation gifs" ON storage.objects;
DROP POLICY IF EXISTS "anon can update neuro segmentation gifs" ON storage.objects;
DROP POLICY IF EXISTS "neuro upload" ON storage.objects;

DROP POLICY IF EXISTS "NeuroQuant users read own segmentation GIFs" ON storage.objects;

-- Upload URLs are minted server-side after credential verification and are
-- scoped to one object. Do not grant anonymous INSERT/UPDATE access.
