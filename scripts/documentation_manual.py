"""Canonical prepared manual block identities and catalog validation."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, kw_only=True)
class ManualScenario:
    """Prepared block identity, not approval of its instructions or execution."""

    identifier: str
    document: str
    field: Literal["fenced_blocks", "tables"]
    sha256: str
    proof_type: Literal["credentialed-integration", "release-cutover"]
    precondition_id: str
    behavioral_verifier: None = None

    @property
    def actor(self) -> Literal["maintainer", "operator"]:
        return "maintainer" if self.proof_type == "credentialed-integration" else "operator"

    @property
    def classification(self) -> str:
        return (
            "credentialed_manual_integration"
            if self.proof_type == "credentialed-integration"
            else "exact_release_cutover_proof"
        )

    @property
    def verification_id(self) -> str:
        return f"manual.{self.proof_type}"

    @property
    def scenario_id(self) -> str:
        return f"{self.proof_type}:{self.identifier}"


# This repo-resident preparation registry is not an independently approved trust
# root. Protected callers must eventually select an immutable approved controller.
MANUAL_SCENARIOS: tuple[ManualScenario, ...] = (
    ManualScenario(
        identifier="src.fieldkit.skills.grill.skill.md.block-1",
        document="src/fieldkit/skills/grill/SKILL.md",
        field="fenced_blocks",
        sha256="70ce593d1231fcf7d2a6d115768fb6b71fa647cba984099278a3c97da60af3e7",
        proof_type="credentialed-integration",
        precondition_id="documented-context:src.fieldkit.skills.grill.skill.md.block-1",
    ),
    ManualScenario(
        identifier="src.fieldkit.skills.grill.skill.md.block-2",
        document="src/fieldkit/skills/grill/SKILL.md",
        field="fenced_blocks",
        sha256="6ed20267feb5aa9d6ebaf5a3e0dd442964884d6902b6abe847d68c751da32539",
        proof_type="credentialed-integration",
        precondition_id="documented-context:src.fieldkit.skills.grill.skill.md.block-2",
    ),
    ManualScenario(
        identifier="src.fieldkit.skills.meeting.skill.md.block-2",
        document="src/fieldkit/skills/meeting/SKILL.md",
        field="fenced_blocks",
        sha256="d214777d4623e09b956d0199226206d39851cd9241c79d3bc0865d327f04c2bb",
        proof_type="credentialed-integration",
        precondition_id="documented-context:src.fieldkit.skills.meeting.skill.md.block-2",
    ),
    ManualScenario(
        identifier="contributing.md.block-1",
        document="CONTRIBUTING.md",
        field="fenced_blocks",
        sha256="7769f7deff0e0af7477e292816063efb24722c38e8b10f29f0d2baae99ebaca5",
        proof_type="release-cutover",
        precondition_id="documented-context:contributing.md.block-1",
    ),
    ManualScenario(
        identifier="contributing.md.block-2",
        document="CONTRIBUTING.md",
        field="fenced_blocks",
        sha256="db5e4c54ea873c77cdfdf036e7948d07e52447de0db19656fad6a21050e57926",
        proof_type="release-cutover",
        precondition_id="documented-context:contributing.md.block-2",
    ),
    ManualScenario(
        identifier="readme.md.block-1",
        document="README.md",
        field="fenced_blocks",
        sha256="4fb449ef8f276bdd57515b4076d4987460537f112469df56b26cc4275780a87d",
        proof_type="release-cutover",
        precondition_id="documented-context:readme.md.block-1",
    ),
    ManualScenario(
        identifier="readme.md.block-3",
        document="README.md",
        field="fenced_blocks",
        sha256="b6b9bd4f55fa4c0266c5b7a3625f02c7d3923128371089f3f8211529da305af7",
        proof_type="release-cutover",
        precondition_id="documented-context:readme.md.block-3",
    ),
    ManualScenario(
        identifier="readme.md.block-4",
        document="README.md",
        field="fenced_blocks",
        sha256="92cee7cfe332837c7bca8da6569042da6f9586496bc30ba694be157f9122fdf6",
        proof_type="release-cutover",
        precondition_id="documented-context:readme.md.block-4",
    ),
    ManualScenario(
        identifier="releasing.md.block-1",
        document="RELEASING.md",
        field="fenced_blocks",
        sha256="0a6138b5aff5ec91e22d1c67ff41ed9b9d8cbca1d2edc1d615fd9568e009d9e7",
        proof_type="release-cutover",
        precondition_id="documented-context:releasing.md.block-1",
    ),
    ManualScenario(
        identifier="releasing.md.block-2",
        document="RELEASING.md",
        field="fenced_blocks",
        sha256="91d306195af669a48193a600aca9ba56e75563c75c04369c8266cea18fb67f13",
        proof_type="release-cutover",
        precondition_id="documented-context:releasing.md.block-2",
    ),
    ManualScenario(
        identifier="releasing.md.block-4",
        document="RELEASING.md",
        field="fenced_blocks",
        sha256="50a2887392175a0bde98a73e4a63398f2644c0204a51eb514358d94235e9ded3",
        proof_type="release-cutover",
        precondition_id="documented-context:releasing.md.block-4",
    ),
    ManualScenario(
        identifier="docs.getting.started.md.block-2",
        document="docs/getting-started.md",
        field="fenced_blocks",
        sha256="4fb449ef8f276bdd57515b4076d4987460537f112469df56b26cc4275780a87d",
        proof_type="release-cutover",
        precondition_id="documented-context:docs.getting.started.md.block-2",
    ),
    ManualScenario(
        identifier="docs.getting.started.md.block-4",
        document="docs/getting-started.md",
        field="fenced_blocks",
        sha256="0f5b8d004a4899dccfbceffeb778d12eb62c8c46430287335a569e6e271f4335",
        proof_type="release-cutover",
        precondition_id="documented-context:docs.getting.started.md.block-4",
    ),
    ManualScenario(
        identifier="docs.getting.started.md.block-9",
        document="docs/getting-started.md",
        field="fenced_blocks",
        sha256="26b3fbfe69cbb7a056b9c83638c983a1cfe4142e2bc79c706877e0d41e41d778",
        proof_type="release-cutover",
        precondition_id="documented-context:docs.getting.started.md.block-9",
    ),
    ManualScenario(
        identifier="docs.guides.gmail.md.block-1",
        document="docs/guides/gmail.md",
        field="fenced_blocks",
        sha256="13f441f859bde4966dccd251fe14c2fd0acdc08d92b5b9655813c62bc320d32c",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.gmail.md.block-1",
    ),
    ManualScenario(
        identifier="docs.guides.gmail.md.block-2",
        document="docs/guides/gmail.md",
        field="fenced_blocks",
        sha256="13f441f859bde4966dccd251fe14c2fd0acdc08d92b5b9655813c62bc320d32c",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.gmail.md.block-2",
    ),
    ManualScenario(
        identifier="docs.guides.gmail.md.block-3",
        document="docs/guides/gmail.md",
        field="fenced_blocks",
        sha256="a3299b4212518da7a523d8027f4bdece18abc8f466e188dd508ece5d8d9bfffc",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.gmail.md.block-3",
    ),
    ManualScenario(
        identifier="docs.guides.gmail.md.block-4",
        document="docs/guides/gmail.md",
        field="fenced_blocks",
        sha256="f1b21beb48a78b83da32c94015d3f50f9ff14dbad2776d38918562a41a0e6159",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.gmail.md.block-4",
    ),
    ManualScenario(
        identifier="docs.guides.gmail.md.block-9",
        document="docs/guides/gmail.md",
        field="fenced_blocks",
        sha256="13f441f859bde4966dccd251fe14c2fd0acdc08d92b5b9655813c62bc320d32c",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.gmail.md.block-9",
    ),
    ManualScenario(
        identifier="docs.guides.gmail.md.block-10",
        document="docs/guides/gmail.md",
        field="fenced_blocks",
        sha256="13f441f859bde4966dccd251fe14c2fd0acdc08d92b5b9655813c62bc320d32c",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.gmail.md.block-10",
    ),
    ManualScenario(
        identifier="docs.guides.gmail.md.block-12",
        document="docs/guides/gmail.md",
        field="fenced_blocks",
        sha256="13f441f859bde4966dccd251fe14c2fd0acdc08d92b5b9655813c62bc320d32c",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.gmail.md.block-12",
    ),
    ManualScenario(
        identifier="docs.guides.morning.brief.md.block-5",
        document="docs/guides/morning-brief.md",
        field="fenced_blocks",
        sha256="5d70c766e4874586f934b85f057179cd96adf6a6f57feae9c1c1e6a612bea0f2",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.morning.brief.md.block-5",
    ),
    ManualScenario(
        identifier="docs.guides.pipeline.workflow.md.block-1",
        document="docs/guides/pipeline-workflow.md",
        field="fenced_blocks",
        sha256="50e533e7bd1e6eed3f4873f0115e27006316b861891223dfa45e2ee6bd6222c0",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.pipeline.workflow.md.block-1",
    ),
    ManualScenario(
        identifier="docs.guides.pipeline.workflow.md.block-2",
        document="docs/guides/pipeline-workflow.md",
        field="fenced_blocks",
        sha256="ce893b5df9d0e4152141ac7402d36c2cc6267f326b9f94c266389f67ca705905",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.pipeline.workflow.md.block-2",
    ),
    ManualScenario(
        identifier="docs.guides.pipeline.workflow.md.block-12",
        document="docs/guides/pipeline-workflow.md",
        field="fenced_blocks",
        sha256="65723de81223ea5b6a579b1a50197eac7ed40a5c342438076133152167a25509",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.pipeline.workflow.md.block-12",
    ),
    ManualScenario(
        identifier="docs.guides.pipeline.workflow.md.block-13",
        document="docs/guides/pipeline-workflow.md",
        field="fenced_blocks",
        sha256="37b46a0d6f53b80312c7bd10ee6505f6bd859835ec2b87a4bb59d3449bc9f169",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.pipeline.workflow.md.block-13",
    ),
    ManualScenario(
        identifier="docs.guides.pipeline.workflow.md.block-14",
        document="docs/guides/pipeline-workflow.md",
        field="fenced_blocks",
        sha256="f10ac8989a6a58919f46ab57f821d734352b36ad9038278d0cdfb8209aa552ac",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.pipeline.workflow.md.block-14",
    ),
    ManualScenario(
        identifier="docs.guides.pipeline.workflow.md.block-15",
        document="docs/guides/pipeline-workflow.md",
        field="fenced_blocks",
        sha256="7603ff8e16a2d0318fc8982ba0a364f07e856379190a873a941d68f6486702c5",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.pipeline.workflow.md.block-15",
    ),
    ManualScenario(
        identifier="docs.guides.pipeline.workflow.md.block-16",
        document="docs/guides/pipeline-workflow.md",
        field="fenced_blocks",
        sha256="5d1f5b0ebcc4ff910ac5443316d372bdfae5b0aebbd0306e02bcee6a2936700d",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.pipeline.workflow.md.block-16",
    ),
    ManualScenario(
        identifier="docs.guides.salesforce.auth.md.block-1",
        document="docs/guides/salesforce-auth.md",
        field="fenced_blocks",
        sha256="a9217b3e234a906e8f22f082d5f5f34d7d72b4c36ce737973afdd443a373788d",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.salesforce.auth.md.block-1",
    ),
    ManualScenario(
        identifier="docs.guides.salesforce.auth.md.block-3",
        document="docs/guides/salesforce-auth.md",
        field="fenced_blocks",
        sha256="69fbb4a8e523ab750f60af4e1a8ea52e4c4bde6271a830bbd2a915267a9fe4fd",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.salesforce.auth.md.block-3",
    ),
    ManualScenario(
        identifier="docs.guides.salesforce.auth.md.block-8",
        document="docs/guides/salesforce-auth.md",
        field="fenced_blocks",
        sha256="001f8aacef3ee8b0f07d95e98244f5dfd3a260459d65ac8e7d4c00fb1090d6ca",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.salesforce.auth.md.block-8",
    ),
    ManualScenario(
        identifier="docs.guides.shadowbot.auth.md.block-1",
        document="docs/guides/shadowbot-auth.md",
        field="fenced_blocks",
        sha256="ae8f861115b69ce35888ba989fc32d5607d9941a8b766c851d9ae44b0c37c4f8",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.shadowbot.auth.md.block-1",
    ),
    ManualScenario(
        identifier="docs.guides.shadowbot.auth.md.block-2",
        document="docs/guides/shadowbot-auth.md",
        field="fenced_blocks",
        sha256="29a29a56fcbd8ea4700cc5970f46a52750986558303e58138087a690279b328d",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.shadowbot.auth.md.block-2",
    ),
    ManualScenario(
        identifier="docs.guides.shadowbot.auth.md.block-3",
        document="docs/guides/shadowbot-auth.md",
        field="fenced_blocks",
        sha256="7cd65829e569a76adeb206fa1dd247c706385d5d4083b5da588c6c8972b7d0d1",
        proof_type="release-cutover",
        precondition_id="documented-context:docs.guides.shadowbot.auth.md.block-3",
    ),
    ManualScenario(
        identifier="docs.guides.shadowbot.auth.md.block-4",
        document="docs/guides/shadowbot-auth.md",
        field="fenced_blocks",
        sha256="d743853c815815f05eca18930e28d08124f7700d4d48bf0e52c1125ea214e286",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.shadowbot.auth.md.block-4",
    ),
    ManualScenario(
        identifier="docs.guides.watchers.md.block-5",
        document="docs/guides/watchers.md",
        field="fenced_blocks",
        sha256="652421d21f5fec8b8bf63549a920dd4cc44c64131c794ff9b971f283843d0b57",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.watchers.md.block-5",
    ),
    ManualScenario(
        identifier="docs.guides.watchers.md.block-8",
        document="docs/guides/watchers.md",
        field="fenced_blocks",
        sha256="5e9e75303faa33dbeee33f977cf42e45c7634ec931246c40e324e07ae92d111f",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.guides.watchers.md.block-8",
    ),
    ManualScenario(
        identifier="docs.integrations.md.block-1",
        document="docs/integrations.md",
        field="fenced_blocks",
        sha256="1956518cdfa0325cc1d7944d6eb3471a057b7e670f0cfef100061946821f7b05",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.integrations.md.block-1",
    ),
    ManualScenario(
        identifier="docs.integrations.md.block-2",
        document="docs/integrations.md",
        field="fenced_blocks",
        sha256="ebd404ccf9adefbf13ac3b3870d30b3218fb56d93ce498d5e4ac68d29d6d55e6",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.integrations.md.block-2",
    ),
    ManualScenario(
        identifier="docs.reference.troubleshooting.md.block-3",
        document="docs/reference/troubleshooting.md",
        field="fenced_blocks",
        sha256="6af47bcc7b3097238894ad56dfddfe1be770eb54a2c180174ce32e04821360c1",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.reference.troubleshooting.md.block-3",
    ),
    ManualScenario(
        identifier="docs.reference.troubleshooting.md.block-4",
        document="docs/reference/troubleshooting.md",
        field="fenced_blocks",
        sha256="ebd404ccf9adefbf13ac3b3870d30b3218fb56d93ce498d5e4ac68d29d6d55e6",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.reference.troubleshooting.md.block-4",
    ),
    ManualScenario(
        identifier="docs.reference.troubleshooting.md.block-6",
        document="docs/reference/troubleshooting.md",
        field="fenced_blocks",
        sha256="bd76af344e70be9c360ccbf406f0be7b562787280be0f76581aef4e058882ff5",
        proof_type="release-cutover",
        precondition_id="documented-context:docs.reference.troubleshooting.md.block-6",
    ),
    ManualScenario(
        identifier="docs.user.guide.md.block-2",
        document="docs/user-guide.md",
        field="fenced_blocks",
        sha256="fbf390db8fd83cdb208554046dce05d45e7b534e8a203cdd19ca71391bc85427",
        proof_type="credentialed-integration",
        precondition_id="documented-context:docs.user.guide.md.block-2",
    ),
    ManualScenario(
        identifier="src.fieldkit.skills.ingest.skill.md.block-4",
        document="src/fieldkit/skills/ingest/SKILL.md",
        field="fenced_blocks",
        sha256="364066c03f49099d1f2dfe2f0c5d38b16e7a5ea1873aedd317775c93a87f771f",
        proof_type="credentialed-integration",
        precondition_id="documented-context:src.fieldkit.skills.ingest.skill.md.block-4",
    ),
    ManualScenario(
        identifier="src.fieldkit.skills.ingest.ops.gmail.refresh.md.block-1",
        document="src/fieldkit/skills/ingest/ops/gmail-refresh.md",
        field="fenced_blocks",
        sha256="9d5bd3d5ceda93a2156dd896cc3527ada99a059f36a99c3ad6f67aeb15fa8730",
        proof_type="credentialed-integration",
        precondition_id="documented-context:src.fieldkit.skills.ingest.ops.gmail.refresh.md.block-1",
    ),
    ManualScenario(
        identifier="src.fieldkit.skills.meeting.ops.stakeholder.map.md.block-4",
        document="src/fieldkit/skills/meeting/ops/stakeholder-map.md",
        field="fenced_blocks",
        sha256="36188ca1d9c5725c79157ebd4947164456527cd223c71136346ccf2c0797231d",
        proof_type="credentialed-integration",
        precondition_id="documented-context:src.fieldkit.skills.meeting.ops.stakeholder.map.md.block-4",
    ),
)


def manual_scenarios(documents: object) -> tuple[ManualScenario, ...]:
    """Compare candidate block identities to the finite controller preparation."""
    if not isinstance(documents, dict):
        raise ValueError("manual scenario registry requires a document mapping")
    expected = {scenario.identifier: scenario for scenario in MANUAL_SCENARIOS}
    if len(expected) != len(MANUAL_SCENARIOS):
        raise ValueError("manual scenario registry has duplicate registrations")
    observed: set[str] = set()
    for document, entry in documents.items():
        if not isinstance(document, str) or not isinstance(entry, dict):
            raise ValueError("manual scenario registry has invalid document entries")
        for field in ("fenced_blocks", "tables"):
            records = entry.get(field, [])
            if not isinstance(records, list):
                raise ValueError("manual scenario registry has invalid block entries")
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("manual scenario registry has invalid block entries")
                identifier = record.get("id")
                verification_id = record.get("verification_id")
                classification = record.get("classification")
                registered = expected.get(identifier) if isinstance(identifier, str) else None
                is_manual = (
                    isinstance(verification_id, str) and verification_id.startswith("manual.")
                ) or classification in ("credentialed_manual_integration", "exact_release_cutover_proof")
                if registered is None and not is_manual:
                    continue
                if (
                    registered is None
                    or identifier in observed
                    or (
                        document != registered.document
                        or field != registered.field
                        or classification != registered.classification
                        or verification_id != registered.verification_id
                        or record.get("sha256") != registered.sha256
                    )
                ):
                    raise ValueError("candidate manual block does not match manual scenario registry")
                observed.add(registered.identifier)
    if observed != set(expected):
        raise ValueError("candidate omits a required manual scenario registry block")
    return MANUAL_SCENARIOS
