import GalleryView, { type GalleryQuery } from './gallery-view';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Gallery -- Dogfood' };

/** Every published event's entered projects, in one place. */
export default async function AllProjectsPage({
  searchParams,
}: {
  searchParams: Promise<GalleryQuery>;
}) {
  return <GalleryView event={null} query={await searchParams} basePath="/gallery" />;
}
