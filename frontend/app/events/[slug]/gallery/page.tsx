import { notFound } from 'next/navigation';

import GalleryView, { type GalleryQuery } from '../../../gallery/gallery-view';
import { apiOrNull } from '@/lib/api';
import type { Event } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Gallery -- Dogfood' };

export default async function EventGalleryPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<GalleryQuery>;
}) {
  const { slug } = await params;
  const event = await apiOrNull<Event>(`/api/events/${slug}`);
  if (!event) notFound();

  return (
    <GalleryView
      event={event}
      query={await searchParams}
      basePath={`/events/${slug}/gallery`}
    />
  );
}
