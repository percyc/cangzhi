import { redirect } from 'next/navigation';

export default function Home() {
  // The root route is authenticated by the proxy. Keep one real application
  // home instead of showing a second, content-free set of navigation links.
  redirect('/documents');
}
