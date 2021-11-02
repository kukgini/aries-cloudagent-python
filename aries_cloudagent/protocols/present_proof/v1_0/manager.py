"""Classes to manage presentations."""

import json
import logging

from ....connections.models.conn_record import ConnRecord
from ....core.error import BaseError
from ....core.profile import Profile
from ....indy.verifier import IndyVerifier
from ....ledger.base import BaseLedger
from ....messaging.decorators.attach_decorator import AttachDecorator
from ....messaging.responder import BaseResponder
from ....storage.error import StorageNotFoundError

from ..indy.pres_exch_handler import IndyPresExchHandler

from .messages.presentation_ack import PresentationAck
from .messages.presentation_problem_report import (
    PresentationProblemReport,
    ProblemReportReason,
)
from .messages.presentation_proposal import PresentationProposal
from .messages.presentation_request import PresentationRequest
from .messages.presentation import Presentation
from .message_types import ATTACH_DECO_IDS, PRESENTATION, PRESENTATION_REQUEST
from .models.presentation_exchange import V10PresentationExchange

LOGGER = logging.getLogger(__name__)


class PresentationManagerError(BaseError):
    """Presentation error."""


class PresentationManager:
    """Class for managing presentations."""

    def __init__(self, profile: Profile):
        """
        Initialize a PresentationManager.

        Args:
            profile: The profile instance for this presentation manager
        """

        self._profile = profile

    async def create_exchange_for_proposal(
        self,
        connection_id: str,
        presentation_proposal_message: PresentationProposal,
        auto_present: bool = None,
    ):
        """
        Create a presentation exchange record for input presentation proposal.

        Args:
            connection_id: connection identifier
            presentation_proposal_message: presentation proposal to serialize
                to exchange record
            auto_present: whether to present proof upon receiving proof request
                (default to configuration setting)

        Returns:
            Presentation exchange record, created

        """
        presentation_exchange_record = V10PresentationExchange(
            connection_id=connection_id,
            thread_id=presentation_proposal_message._thread_id,
            initiator=V10PresentationExchange.INITIATOR_SELF,
            role=V10PresentationExchange.ROLE_PROVER,
            state=V10PresentationExchange.STATE_PROPOSAL_SENT,
            presentation_proposal_dict=presentation_proposal_message,
            auto_present=auto_present,
            trace=(presentation_proposal_message._trace is not None),
        )
        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="create presentation proposal"
            )

        return presentation_exchange_record

    async def receive_proposal(
        self, message: PresentationProposal, connection_record: ConnRecord
    ):
        """
        Receive a presentation proposal from message in context on manager creation.

        Returns:
            Presentation exchange record, created

        """
        presentation_exchange_record = V10PresentationExchange(
            connection_id=connection_record.connection_id,
            thread_id=message._thread_id,
            initiator=V10PresentationExchange.INITIATOR_EXTERNAL,
            role=V10PresentationExchange.ROLE_VERIFIER,
            state=V10PresentationExchange.STATE_PROPOSAL_RECEIVED,
            presentation_proposal_dict=message,
            trace=(message._trace is not None),
        )
        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="receive presentation request"
            )

        return presentation_exchange_record

    async def create_bound_request(
        self,
        presentation_exchange_record: V10PresentationExchange,
        name: str = None,
        version: str = None,
        nonce: str = None,
        comment: str = None,
    ):
        """
        Create a presentation request bound to a proposal.

        Args:
            presentation_exchange_record: Presentation exchange record for which
                to create presentation request
            name: name to use in presentation request (None for default)
            version: version to use in presentation request (None for default)
            nonce: nonce to use in presentation request (None to generate)
            comment: Optional human-readable comment pertaining to request creation

        Returns:
            A tuple (updated presentation exchange record, presentation request message)

        """
        indy_proof_request = await (
            presentation_exchange_record.presentation_proposal_dict
        ).presentation_proposal.indy_proof_request(
            name=name,
            version=version,
            nonce=nonce,
            ledger=self._profile.inject(BaseLedger),
        )
        presentation_request_message = PresentationRequest(
            comment=comment,
            request_presentations_attach=[
                AttachDecorator.data_base64(
                    mapping=indy_proof_request,
                    ident=ATTACH_DECO_IDS[PRESENTATION_REQUEST],
                )
            ],
        )
        presentation_request_message._thread = {
            "thid": presentation_exchange_record.thread_id
        }
        presentation_request_message.assign_trace_decorator(
            self._profile.settings, presentation_exchange_record.trace
        )

        presentation_exchange_record.thread_id = presentation_request_message._thread_id
        presentation_exchange_record.state = V10PresentationExchange.STATE_REQUEST_SENT
        presentation_exchange_record.presentation_request = indy_proof_request
        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="create (bound) presentation request"
            )

        return presentation_exchange_record, presentation_request_message

    async def create_exchange_for_request(
        self, connection_id: str, presentation_request_message: PresentationRequest
    ):
        """
        Create a presentation exchange record for input presentation request.

        Args:
            connection_id: connection identifier
            presentation_request_message: presentation request to use in creating
                exchange record, extracting indy proof request and thread id

        Returns:
            Presentation exchange record, updated

        """
        presentation_exchange_record = V10PresentationExchange(
            connection_id=connection_id,
            thread_id=presentation_request_message._thread_id,
            initiator=V10PresentationExchange.INITIATOR_SELF,
            role=V10PresentationExchange.ROLE_VERIFIER,
            state=V10PresentationExchange.STATE_REQUEST_SENT,
            presentation_request=presentation_request_message.indy_proof_request(),
            presentation_request_dict=presentation_request_message,
            trace=(presentation_request_message._trace is not None),
        )
        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="create (free) presentation request"
            )

        return presentation_exchange_record

    async def receive_request(
        self, presentation_exchange_record: V10PresentationExchange
    ):
        """
        Receive a presentation request.

        Args:
            presentation_exchange_record: presentation exchange record with
                request to receive

        Returns:
            The presentation_exchange_record, updated

        """
        presentation_exchange_record.state = (
            V10PresentationExchange.STATE_REQUEST_RECEIVED
        )
        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="receive presentation request"
            )

        return presentation_exchange_record

    async def create_presentation(
        self,
        presentation_exchange_record: V10PresentationExchange,
        requested_credentials: dict,
        comment: str = None,
    ):
        """
        Create a presentation.

        Args:
            presentation_exchange_record: Record to update
            requested_credentials: Indy formatted requested_credentials
            comment: optional human-readable comment


        Example `requested_credentials` format, mapping proof request referents (uuid)
        to wallet referents (cred id):

        ::

            {
                "self_attested_attributes": {
                    "j233ffbc-bd35-49b1-934f-51e083106f6d": "value"
                },
                "requested_attributes": {
                    "6253ffbb-bd35-49b3-934f-46e083106f6c": {
                        "cred_id": "5bfa40b7-062b-4ae0-a251-a86c87922c0e",
                        "revealed": true
                    }
                },
                "requested_predicates": {
                    "bfc8a97d-60d3-4f21-b998-85eeabe5c8c0": {
                        "cred_id": "5bfa40b7-062b-4ae0-a251-a86c87922c0e"
                    }
                }
            }

        Returns:
            A tuple (updated presentation exchange record, presentation message)

        """
        indy_handler = IndyPresExchHandler(self._profile)
        indy_proof = await indy_handler.return_presentation(
            pres_ex_record=presentation_exchange_record,
            requested_credentials=requested_credentials,
        )

        presentation_message = Presentation(
            comment=comment,
            presentations_attach=[
                AttachDecorator.data_base64(
                    mapping=indy_proof, ident=ATTACH_DECO_IDS[PRESENTATION]
                )
            ],
        )

        presentation_message._thread = {"thid": presentation_exchange_record.thread_id}
        presentation_message.assign_trace_decorator(
            self._profile.settings, presentation_exchange_record.trace
        )

        # save presentation exchange state
        presentation_exchange_record.state = (
            V10PresentationExchange.STATE_PRESENTATION_SENT
        )
        presentation_exchange_record.presentation = indy_proof
        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="create presentation"
            )

        return presentation_exchange_record, presentation_message

    async def receive_presentation(
        self, message: Presentation, connection_record: ConnRecord
    ):
        """
        Receive a presentation, from message in context on manager creation.

        Returns:
            presentation exchange record, retrieved and updated

        """
        presentation = message.indy_proof()

        thread_id = message._thread_id
        connection_id_filter = (
            {"connection_id": connection_record.connection_id}
            if connection_record is not None
            else None
        )
        async with self._profile.session() as session:
            try:
                (
                    presentation_exchange_record
                ) = await V10PresentationExchange.retrieve_by_tag_filter(
                    session, {"thread_id": thread_id}, connection_id_filter
                )
            except StorageNotFoundError:
                # Proof Request not bound to any connection: requests_attach in OOB msg
                (
                    presentation_exchange_record
                ) = await V10PresentationExchange.retrieve_by_tag_filter(
                    session, {"thread_id": thread_id}, None
                )

        # Check for bait-and-switch in presented attribute values vs. proposal
        if presentation_exchange_record.presentation_proposal_dict:
            exchange_pres_proposal = (
                presentation_exchange_record.presentation_proposal_dict
            )
            presentation_preview = exchange_pres_proposal.presentation_proposal

            proof_req = presentation_exchange_record._presentation_request.ser
            for (reft, attr_spec) in presentation["requested_proof"][
                "revealed_attrs"
            ].items():
                name = proof_req["requested_attributes"][reft]["name"]
                value = attr_spec["raw"]
                if not presentation_preview.has_attr_spec(
                    cred_def_id=presentation["identifiers"][
                        attr_spec["sub_proof_index"]
                    ]["cred_def_id"],
                    name=name,
                    value=value,
                ):
                    presentation_exchange_record.state = None
                    async with self._profile.session() as session:
                        await presentation_exchange_record.save(
                            session,
                            reason=(
                                f"Presentation {name}={value} mismatches proposal value"
                            ),
                        )
                    raise PresentationManagerError(
                        f"Presentation {name}={value} mismatches proposal value"
                    )

        presentation_exchange_record.presentation = presentation
        presentation_exchange_record.state = (
            V10PresentationExchange.STATE_PRESENTATION_RECEIVED
        )

        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="receive presentation"
            )

        return presentation_exchange_record

    async def verify_presentation(
        self, presentation_exchange_record: V10PresentationExchange
    ):
        """
        Verify a presentation.

        Args:
            presentation_exchange_record: presentation exchange record
                with presentation request and presentation to verify

        Returns:
            presentation record, updated

        """
        indy_proof_request = presentation_exchange_record._presentation_request.ser
        indy_proof = presentation_exchange_record._presentation.ser
        indy_handler = IndyPresExchHandler(self._profile)
        (
            schemas,
            cred_defs,
            rev_reg_defs,
            rev_reg_entries,
        ) = await indy_handler.process_pres_identifiers(indy_proof["identifiers"])

        verifier = self._profile.inject(IndyVerifier)
        presentation_exchange_record.verified = json.dumps(  # tag: needs string value
            await verifier.verify_presentation(
                dict(
                    indy_proof_request
                ),  # copy to avoid changing the proof req in the stored pres exch
                indy_proof,
                schemas,
                cred_defs,
                rev_reg_defs,
                rev_reg_entries,
            )
        )
        presentation_exchange_record.state = V10PresentationExchange.STATE_VERIFIED

        async with self._profile.session() as session:
            await presentation_exchange_record.save(
                session, reason="verify presentation"
            )

        await self.send_presentation_ack(presentation_exchange_record)
        return presentation_exchange_record

    async def send_presentation_ack(
        self, presentation_exchange_record: V10PresentationExchange
    ):
        """
        Send acknowledgement of presentation receipt.

        Args:
            presentation_exchange_record: presentation exchange record with thread id

        """
        responder = self._profile.inject_or(BaseResponder)

        if responder:
            presentation_ack_message = PresentationAck()
            presentation_ack_message._thread = {
                "thid": presentation_exchange_record.thread_id
            }
            presentation_ack_message.assign_trace_decorator(
                self._profile.settings, presentation_exchange_record.trace
            )

            await responder.send_reply(
                presentation_ack_message,
                connection_id=presentation_exchange_record.connection_id,
            )
        else:
            LOGGER.warning(
                "Configuration has no BaseResponder: cannot ack presentation on %s",
                presentation_exchange_record.thread_id,
            )

    async def receive_presentation_ack(
        self, message: PresentationAck, connection_record: ConnRecord
    ):
        """
        Receive a presentation ack, from message in context on manager creation.

        Returns:
            presentation exchange record, retrieved and updated

        """
        async with self._profile.session() as session:
            (
                presentation_exchange_record
            ) = await V10PresentationExchange.retrieve_by_tag_filter(
                session,
                {"thread_id": message._thread_id},
                {"connection_id": connection_record.connection_id},
            )

            presentation_exchange_record.state = (
                V10PresentationExchange.STATE_PRESENTATION_ACKED
            )

            await presentation_exchange_record.save(
                session, reason="receive presentation ack"
            )

        return presentation_exchange_record

    async def receive_problem_report(
        self, message: PresentationProblemReport, connection_id: str
    ):
        """
        Receive problem report.

        Returns:
            presentation exchange record, retrieved and updated

        """
        # FIXME use transaction, fetch for_update
        async with self._profile.session() as session:
            pres_ex_record = await (
                V10PresentationExchange.retrieve_by_tag_filter(
                    session,
                    {"thread_id": message._thread_id},
                    {"connection_id": connection_id},
                )
            )

            pres_ex_record.state = None
            code = message.description.get("code", ProblemReportReason.ABANDONED.value)
            pres_ex_record.error_msg = f"{code}: {message.description.get('en', code)}"
            await pres_ex_record.save(session, reason="received problem report")

        return pres_ex_record
