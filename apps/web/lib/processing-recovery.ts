/** Recover only explicitly failed base stages; never call full reprocessing. */
export async function retryFailedDocuments(documentIds: number[], request: typeof fetch = fetch) {
  const results = await Promise.allSettled(documentIds.map(async (id) => {
    const response = await request(`/api/documents/${id}/retry-failed`, { method: 'POST' });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(`资料 ${id}：${typeof body.detail === 'string' ? body.detail : '重试提交失败'}`);
    }
    return Array.isArray(body.job_ids) && body.job_ids.length > 0;
  }));
  return {
    submitted: results.filter((result) => result.status === 'fulfilled' && result.value).length,
    unchanged: results.filter((result) => result.status === 'fulfilled' && !result.value).length,
    failures: results.flatMap((result, index) => result.status === 'rejected'
      ? [{ id: documentIds[index], message: result.reason instanceof Error ? result.reason.message : '重试提交失败' }]
      : []),
  };
}
