import Dashboard from "@/components/dashboard";
export default async function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Dashboard projectId={id} />;
}
