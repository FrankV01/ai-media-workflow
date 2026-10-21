"""
app.models — SQLAlchemy ORM models

Pipeline tracking: Job (incl. warnings), JobQualityControl, JobStep (app/models/job.py)
Creative audit trail + LLM profiles: CreativeRole, LlmRoleConfiguration,
LlmConfigurationChange (save/reset mutation history), RoleExecution,
Message (app/models/creative.py)
Media profiles + audit: MediaModelConfiguration, MediaGenerationExecution
(app/models/media.py)
Generic per-workflow block profiles: BlockConfiguration (app/models/block.py)
Configuration: Setting (app/models/setting.py)
Delivery audit: DeliveryArchive, DeliveryArchiveEntry (app/models/delivery.py)
Storefront audit: StorefrontListing (app/models/storefront.py)
Cloud upload audit: CloudflareUpload (app/models/cloudflare.py)
Scheduled pins: ScheduledPin (app/models/pinterest.py)
"""
