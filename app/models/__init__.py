"""
app.models — SQLAlchemy ORM models

Pipeline tracking: Job (incl. warnings), JobStep (app/models/job.py)
Creative audit trail + LLM profiles: CreativeRole, LlmRoleConfiguration,
LlmConfigurationChange (save/reset mutation history), RoleExecution,
Message (app/models/creative.py)
Media profiles + audit: MediaModelConfiguration, MediaGenerationExecution
(app/models/media.py)
Configuration: Setting (app/models/setting.py)
"""
