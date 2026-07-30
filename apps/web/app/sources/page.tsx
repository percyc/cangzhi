import { redirect } from 'next/navigation';

export default function LegacySourcesPage() {
  redirect('/settings/sources');
}
