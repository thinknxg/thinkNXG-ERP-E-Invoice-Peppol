"""ConvergeXStatusService: provider status -> ERP status, kept apart from the
HTTP layer so it can be tested on its own."""


class ConvergeXStatusService:
	def __init__(self, contract):
		self.status_map = {k.lower(): v for k, v in (contract.get("status_map") or {}).items()}
		self.final = {s.lower() for s in contract.get("final_provider_statuses") or []}
		self.job_map = {k.lower(): v for k, v in (contract.get("job_status_map") or {}).items()}

	def to_reporting_status(self, provider_status):
		"""ConvergeX status -> custom_reporting_status. An unknown status is
		passed through in lower case rather than guessed into a final one."""
		if not provider_status:
			return None
		raw = provider_status.strip().lower()
		return self.status_map.get(raw, raw)

	def is_final(self, provider_status):
		return bool(provider_status) and provider_status.strip().lower() in self.final

	def is_rejected_or_failed(self, provider_status):
		return self.to_reporting_status(provider_status) in ("rejected", "failed")

	def job_state(self, job_status):
		"""pending | completed | failed; unknown words stay pending (keep polling)."""
		if not job_status:
			return "pending"
		return self.job_map.get(job_status.strip().lower(), "pending")
