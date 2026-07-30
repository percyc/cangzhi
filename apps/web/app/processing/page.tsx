import { redirect } from 'next/navigation';

export default function LegacyProcessingPage() {
  redirect('/inbox');
}
