-- Radiologist uploads use the existing `neuro` bucket and remain under
-- radiologist/<display-name>/scans/...
--
-- IMPORTANT: These policies allow unauthenticated (anon) list/delete calls
-- within the radiologist upload prefixes. The local account is a UI filter,
-- NOT a secure identity. The `neuro` bucket is currently public, so anyone who
-- obtains an object URL can download that object. Do not use this arrangement
-- for identifiable patient scans without accepting that exposure risk.

drop policy if exists "Radiologists upload scans to their own folder" on storage.objects;
drop policy if exists "Radiologists view their own uploaded scans" on storage.objects;
drop policy if exists "Radiologists delete their own uploaded scans" on storage.objects;
drop policy if exists "Radiologist local upload neuro" on storage.objects;
drop policy if exists "Radiologist local upload list neuro" on storage.objects;
drop policy if exists "Radiologist local upload delete neuro" on storage.objects;

create policy "Radiologist local upload neuro"
on storage.objects
for insert
to anon
with check (
  bucket_id = 'neuro'
  and (storage.foldername(name))[1] in ('radiologist', 'radiologist_uploads')
);

create policy "Radiologist local upload list neuro"
on storage.objects
for select
to anon
using (
  bucket_id = 'neuro'
  and (storage.foldername(name))[1] in ('radiologist', 'radiologist_uploads')
);

create policy "Radiologist local upload delete neuro"
on storage.objects
for delete
to anon
using (
  bucket_id = 'neuro'
  and (storage.foldername(name))[1] in ('radiologist', 'radiologist_uploads')
);
