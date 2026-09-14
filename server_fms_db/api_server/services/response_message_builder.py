from __future__ import annotations
from shared.enums.ai import Intent
from shared.models.factory import PendingProductionRequest, RoofOptionCode
from shared.services.production_status_query_service import ProductionStatusQueryResult
from shared.models.factory import JobStatus, ProductionJobControlState
class ResponseMessageBuilder:
 def build_interpretation(self, command) -> str:
  """Safe acknowledgement after interpretation; never claims an action ran."""
  if command.clarification_needed: return command.clarification_message or "추가 정보를 말씀해 주세요."
  if command.intent == Intent.CREATE_PRODUCTION_REQUEST:
   name=command.product_name or "초소형 주택"; q=command.quantity or 1
   return f"{name} {'한 채' if q==1 else str(q)+'채'} 생산 요청을 확인했습니다. 실제 실행 전 승인이 필요합니다."
  if command.intent in {Intent.PAUSE_JOB, Intent.RESUME_JOB, Intent.CANCEL_JOB}:
   labels={Intent.PAUSE_JOB:'일시정지',Intent.RESUME_JOB:'재개',Intent.CANCEL_JOB:'취소'}
   return f"현재 작업 {labels[command.intent]} 요청을 확인했습니다. 실제 적용 전 승인이 필요합니다."
  if command.intent == Intent.QUERY_JOB_STATUS: return "현재 생산 작업 상태를 조회하는 요청입니다."
  if command.intent == Intent.QUERY_INVENTORY: return "현재 재고를 조회하는 요청입니다."
  return "현재 지원하지 않는 요청입니다."

 def build(self, intent: Intent, result: dict) -> str:
  success=result.get("success", True); reason=result.get("reason")
  if intent == Intent.CREATE_PRODUCTION_REQUEST:
   if not success: return f"{reason or '요청을 처리할 수 없어'} 생산을 시작할 수 없습니다."
   name=result.get("product_name","초소형 주택"); q=result.get("quantity",1); return f"{name} {'한 채' if q==1 else str(q)+'채'} 생산을 시작하겠습니다."
  if intent in {Intent.PAUSE_JOB,Intent.RESUME_JOB,Intent.CANCEL_JOB}:
   verb={Intent.PAUSE_JOB:'일시정지',Intent.RESUME_JOB:'재개',Intent.CANCEL_JOB:'취소'}[intent]; return f"현재 생산 작업을 {verb}{'했습니다' if success else '할 수 없습니다'} .".replace(' .','.')
  if intent == Intent.QUERY_JOB_STATUS:
   return f"현재 {result.get('product_name','생산 작업')}의 {result.get('current_step','진행')} 단계가 진행 중이며, 전체 공정은 약 {result.get('progress',0)}퍼센트 완료되었습니다."
  if intent == Intent.QUERY_INVENTORY:
   if result.get('items'): return '현재 ' + ', '.join(f"{x['name']}은 {x['quantity']}개" for x in result['items'][:5]) + ' 남아 있습니다.'
   return f"현재 {result.get('item_name','해당 품목')}은 {result.get('quantity',0)}개 남아 있습니다."
  return '현재 지원하지 않는 요청입니다.'


 def build_roof_selection_question(self) -> str:
  return "지붕을 선택해주세요. 1번 평지붕, 2번 경사지붕입니다."

 def build_pending_confirmation(self, pending: PendingProductionRequest) -> str:
  product_names={"HOUSE_A":"A형 주택", "HOUSE_B":"B형 주택"}
  roof_names={RoofOptionCode.ROOF_01:"1번 평지붕", RoofOptionCode.ROOF_02:"2번 경사지붕"}
  quantity_names={1:"한 채", 2:"두 채"}
  product_name=product_names.get(pending.product_code, pending.product_code)
  quantity=quantity_names.get(pending.quantity, f"{pending.quantity}채")
  roof_name=roof_names[pending.roof_option_code]
  return f"{product_name} {quantity}를 {roof_name}으로 제작하는 것이 맞습니까?"

 def build_pending_confirmed(self, *, job_count: int = 0) -> str:
  if job_count == 1: return "생산 요청이 확인되어 생산 작업 1건이 등록되었습니다."
  if job_count > 1: return f"생산 요청이 확인되어 생산 작업 {job_count}건이 등록되었습니다."
  return "생산 요청이 확인되었습니다."

 def build_pending_rejected(self) -> str:
  return "생산 요청을 진행하지 않겠습니다."

 def build_shortage_message(self, product_code: str, shortages: list) -> str:
  product_names={"HOUSE_A":"A형 주택", "HOUSE_B":"B형 주택"}
  product_name = product_names.get(product_code, product_code)
  parts = [f"{s.part_name} {s.shortage_quantity}개" for s in shortages]
  return f"현재 {product_name} 생산에 필요한 {', '.join(parts)}가 부족하여 생산할 수 없습니다."

 def build_configuration_invalid_message(self, product_code: str) -> str:
  product_names={"HOUSE_A":"A형 주택", "HOUSE_B":"B형 주택"}
  product_name = product_names.get(product_code, product_code)
  return f"현재 {product_name}의 생산 자재 설정이 완료되지 않아 생산 요청을 진행할 수 없습니다."

 def build_inventory_narration(self, command, inventory_result) -> str:
  """Narrate master-scoped Voice inventory using available, not physical, stock."""
  if command.inventory_scope == "CATEGORY":
   return "현재 카테고리별 재고 조회는 지원되지 않습니다."
  rows = list(inventory_result or [])
  if command.item_name and not rows:
   return f"{command.item_name}에 해당하는 자재를 찾을 수 없습니다."
  if not rows:
   return "현재 등록된 생산 자재 재고가 없습니다."

  def product_contexts(item):
   return tuple(
    sorted(
     (
      (getattr(product, "product_code", ""), getattr(product, "product_name", None) or getattr(product, "product_code", ""))
      for product in (getattr(item, "products", ()) or ())
     ),
     key=lambda context: (context[1], context[0]),
    )
   )

  def item_name(item):
   return getattr(item, "part_name", None) or getattr(item, "part_code", "자재")

  def item_availability(item):
   quantity = getattr(item, "quantity", 0)
   reserved = getattr(item, "reserved_quantity", 0)
   available = getattr(item, "available_quantity", 0)
   if available <= 0:
    return "zero", f"{item_name(item)}은 현재 사용 가능한 재고가 없습니다"
   if reserved > 0:
    return "reserved", f"{item_name(item)}은 총 {quantity}개 중 {reserved}개가 예약되어 현재 {available}개"
   return "available", f"{item_name(item)} {available}개"

  def single_sentence(item, product_name=""):
   kind, detail = item_availability(item)
   subject = f"{product_name} {item_name(item)}".strip()
   if kind == "zero":
    return f"{subject}은 현재 사용 가능한 재고가 없습니다"
   if kind == "reserved":
    quantity = getattr(item, "quantity", 0)
    reserved = getattr(item, "reserved_quantity", 0)
    available = getattr(item, "available_quantity", 0)
    return f"{subject}은 총 {quantity}개 중 {reserved}개가 예약되어 현재 {available}개 사용 가능합니다"
   return f"{subject}은 현재 {getattr(item, 'available_quantity', 0)}개 사용 가능합니다"

  def query_label(product_code, product_name):
   """Remove master-derived scope text only for natural grouped narration."""
   term = (command.item_name or "").strip()
   if not term:
    return ""
   candidates = [product_name, product_name.split()[0] if product_name else "", product_code]
   folded = term.casefold()
   for candidate in candidates:
    if candidate and folded.startswith(candidate.casefold()):
     remainder = term[len(candidate):].strip()
     if remainder:
      return remainder
   return term

  # Option stages express compatibility, not Product-exclusive ownership.
  # Keep their product associations in the structured response, but omit that
  # context from an unqualified narration. Legacy no-master rows remain on the
  # existing single-row wording path.
  groups = {}
  option_items = []
  legacy_unscoped = []
  for item in rows:
   if getattr(item, "is_option_material", False):
    option_items.append(item)
    continue
   contexts = product_contexts(item)
   if not contexts:
    legacy_unscoped.append(item)
    continue
   for context in contexts:
    groups.setdefault(context, []).append(item)

  sentences = []
  for (product_code, product_name), items in sorted(groups.items(), key=lambda group: (group[0][1], group[0][0])):
   items = sorted(items, key=lambda item: getattr(item, "part_code", ""))
   if len(items) == 1:
    sentences.append(single_sentence(items[0], product_name))
    continue

   label = query_label(product_code, product_name)
   subject = f"{product_name} {label} 재고".strip() if label else f"{product_name} 재고"
   details = [item_availability(item) for item in items]
   if all(kind == "available" for kind, _detail in details):
    sentences.append(f"{subject}는 {', '.join(detail for _kind, detail in details)}가 사용 가능합니다")
   else:
    sentences.append(f"{subject}는 {', '.join(detail for _kind, detail in details)}입니다")

  option_items = sorted(option_items, key=lambda item: getattr(item, "part_code", ""))
  if len(option_items) == 1:
   sentences.append(single_sentence(option_items[0]))
  elif option_items:
   label = (command.item_name or "옵션 자재").strip()
   # A product-scoped option query limits compatibility candidates, but the
   # spoken label must not imply that the option is product-exclusive.
   for product_code, product_name in product_contexts(option_items[0]):
    scoped_label = query_label(product_code, product_name)
    if scoped_label != label:
     label = scoped_label
     break
   details = [item_availability(item) for item in option_items]
   if all(kind == "available" for kind, _detail in details):
    sentences.append(f"{label} 재고는 {', '.join(detail for _kind, detail in details)}가 사용 가능합니다")
   else:
    sentences.append(f"{label} 재고는 {', '.join(detail for _kind, detail in details)}입니다")

  for item in sorted(legacy_unscoped, key=lambda item: getattr(item, "part_code", "")):
   sentences.append(single_sentence(item))

  if command.item_name:
   return ". ".join(sentences) + "."
  return "현재 생산 자재 재고는 " + ". ".join(sentences) + "."

 def build_job_status_message(self, result: ProductionStatusQueryResult | None) -> str:
  if result is None:
   return "현재 진행 중인 생산 작업이 없습니다."
  name = result.product_name
  step = result.current_step_name
  if result.control_state == ProductionJobControlState.PAUSE_REQUESTED:
   return f"현재 {name} 생산의 일시정지 요청을 처리하고 있습니다."
  if result.control_state == ProductionJobControlState.PAUSED:
   return f"현재 {name} 생산은 일시정지 상태입니다."
  if result.control_state == ProductionJobControlState.RESUME_REQUESTED:
   return f"현재 {name} 생산의 재개 요청을 처리하고 있습니다."
  if result.status == JobStatus.COMPLETED:
   return f"최근 {name} 생산은 완료되었습니다."
  elif result.status == JobStatus.CANCELED:
   return f"해당 {name} 생산 작업은 취소되었습니다."
  elif result.status == JobStatus.FAILED:
   step_str = f" {step} 단계에서" if step else ""
   return f"{name} 생산이 중단되었습니다.{step_str} 오류가 발생했습니다."
  elif result.status in (JobStatus.READY, JobStatus.REQUESTED, JobStatus.ROOF_READY) and not step:
   return f"{name} 생산 준비가 완료되어 작업 시작을 기다리고 있습니다."
  else:
   step_str = f" {step} 공정을 진행하고 있습니다." if step else " 작업을 진행하고 있습니다."
   particle = "를" if name.endswith("하우스") else "을"
   return f"현재 {name}{particle} 생산 중이며{step_str}"
