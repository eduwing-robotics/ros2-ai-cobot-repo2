from fms_server.incoming_material_qa_client import IncomingMaterialQAClient
from shared.schemas.vision import IncomingMaterialQARequest, IncomingMaterialQAResult

class UdpIncomingMaterialQAClient(IncomingMaterialQAClient):
    """
    Placeholder for the future UDP-based Vision transport.
    DO NOT IMPLEMENT REAL UDP SOCKETS YET. Protocol is NOT FINAL.
    """
    def request_inspection(
        self, request: IncomingMaterialQARequest, timeout_sec: float = 10.0
    ) -> IncomingMaterialQAResult:
        raise NotImplementedError("REAL VISION TRANSPORT NOT IMPLEMENTED. UDP CONTRACT NOT FINALIZED.")
