from __future__ import annotations
from enum import Enum
from typing import List, Optional, Any, Annotated, Union
import re
import math
import uuid
from pydantic import AliasChoices, BaseModel, Field, ConfigDict, field_validator, ValidationInfo, BeforeValidator, model_validator

def generate_id() -> str:
    return uuid.uuid4().hex[:8]

def normalize_yes_no(v: Any) -> Any:
    if v is None:
        return 'null'
    if isinstance(v, dict) and 'v' in v:
        v = v['v']
    if isinstance(v, bool):
        return 'yes' if v else 'no'
    if isinstance(v, str):
        v_lower = v.lower().strip()
        if v_lower in ['sealed', 'closed', 'yes', 'true', 'capped', 'deoxygenated', 'oxygenated']:
            return 'yes'
        if v_lower in ['open', 'no', 'false', 'unsealed', 'aerated', 'air-equilibrated']:
            return 'no'
        if v_lower in ['n/a', 'null', 'unknown', 'none', 'not reported']:
            return 'null'
    return 'null'

def _normalize_gas_text(v: Any) -> str:
    if v is None:
        return ''
    if isinstance(v, dict) and 'v' in v:
        v = v['v']
    if isinstance(v, bool):
        return 'yes' if v else 'no'
    return str(v).lower().strip()

def normalize_deoxygenated(v: Any) -> Any:
    text = _normalize_gas_text(v)
    if not text:
        return 'null'
    if text in ['yes', 'true', '1']:
        return 'yes'
    if text in ['no', 'false', '0']:
        return 'no'
    if any((marker in text for marker in ['deoxygenated', 'deaerated', 'oxygen-free', 'oxygen free', 'n2', 'nitrogen', 'argon', 'ar-purged', 'ar purged', 'inert gas', 'degassed'])):
        return 'yes'
    if any((marker in text for marker in ['oxygenated', 'oxygen-saturated', 'oxygen saturated', 'o2', 'air-saturated', 'air saturated', 'air-equilibrated', 'air equilibrated', 'aerated', 'bubbled with air'])):
        return 'no'
    if text in ['n/a', 'null', 'unknown', 'none', 'not reported']:
        return 'null'
    return 'null'

def normalize_oxygenated(v: Any) -> Any:
    text = _normalize_gas_text(v)
    if not text:
        return 'null'
    if text in ['yes', 'true', '1']:
        return 'yes'
    if text in ['no', 'false', '0']:
        return 'no'
    if any((marker in text for marker in ['oxygenated', 'oxygen-saturated', 'oxygen saturated', 'o2', 'air-saturated', 'air saturated', 'air-equilibrated', 'air equilibrated', 'aerated', 'bubbled with air'])):
        return 'yes'
    if any((marker in text for marker in ['deoxygenated', 'deaerated', 'oxygen-free', 'oxygen free', 'n2', 'nitrogen', 'argon', 'ar-purged', 'ar purged', 'inert gas', 'degassed'])):
        return 'no'
    if text in ['n/a', 'null', 'unknown', 'none', 'not reported']:
        return 'null'
    return 'null'

def extract_float(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (float, int)):
        return float(v)
    if isinstance(v, str):
        v_clean = v.replace(',', '').strip()
        v_lower = v_clean.lower()
        if v_lower in ['n/a', 'none', 'null', 'nan', '-', '', 'nd', 'not detected']:
            return None
        match = re.search('[-+]?\\d*\\.?\\d+([eE][-+]?\\d+)?', v_clean)
        if match:
            try:
                return float(match.group())
            except ValueError:
                return None
    return v

def force_string(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return str(v)
    return str(v).strip()

def normalize_category_fuzzy(v: Any) -> Any:
    if isinstance(v, str):
        v_clean = v.strip().lower().replace('-', '').replace('_', '').replace(' ', '')
        mapping = {'rateconstant': 'kp', 'kobs': 'kp', 'kp': 'kp', 'kdeg': 'kp', 'halflife': 'hl', 't1/2': 'hl', 'hl': 'hl', 'quantumyield': 'qy', 'qy': 'qy', 'phi': 'qy', 'koh': 'k_OH', 'ohradical': 'k_OH', 'k_oh': 'k_OH', 'k1o2': 'k_1O2', 'singletoxygen': 'k_1O2', 'k_1o2': 'k_1O2', 'k3dom': 'k_3DOM', 'triplet': 'k_3DOM', 'k_3dom': 'k_3DOM', 'other': 'other', 'otherparameter': 'other'}
        if v_clean in mapping:
            return mapping[v_clean]
        for key, val in mapping.items():
            if key in v_clean:
                return val
    return v

def normalize_src_list(v: Any) -> List[int]:
    if v is None:
        return []
    if isinstance(v, int):
        return [v]
    vals = []
    if isinstance(v, list):
        raw_list = v
    elif isinstance(v, str):
        raw_list = re.findall('\\d+', v)
    else:
        return []
    for item in raw_list:
        try:
            vals.append(int(str(item).strip()))
        except (ValueError, TypeError):
            continue
    return sorted(list(set(vals)))
RobustFloat = Annotated[float, BeforeValidator(extract_float)]
RobustString = Annotated[str, BeforeValidator(force_string)]
RobustYesNo = Annotated[str, BeforeValidator(normalize_yes_no)]
RobustCategory = Annotated[str, BeforeValidator(normalize_category_fuzzy)]
RobustSrc = Annotated[List[int], BeforeValidator(normalize_src_list)]
SrcType = Annotated[RobustSrc, Field(min_length=1, description='List of paragraph IDs providing evidence (e.g., [1, 5]).')]

class BaseConfigModel(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)

class Txt(BaseConfigModel):
    v: Optional[RobustString] = Field(None, description='Extracted text value.')
    src: SrcType

    @field_validator('src')
    @classmethod
    def validate_source_exists(cls, v: List[int], info: ValidationInfo):
        context = info.context
        if context and 'valid_ids' in context:
            valid_ids = context['valid_ids']
            bad_ids = [i for i in v if i not in valid_ids]
            if bad_ids:
                raise ValueError(f'IDs {bad_ids} not found in text.')
        return v

class Num(BaseConfigModel):
    v: RobustFloat = Field(..., description='Mantissa.')
    e: int = Field(0, description='Exponent.')
    u: Optional[RobustString] = Field(None, description='Unit.')
    sd: Optional[RobustFloat] = Field(None, description='Std Dev.')
    min: Optional[RobustFloat] = Field(None, description='Range min.')
    max: Optional[RobustFloat] = Field(None, description='Range max.')
    desc: Optional[RobustString] = Field(None, description='Qualifiers.')
    src: SrcType

class ChemicalTypeEnum(str, Enum):
    pure_substance = 'pure_substance'
    mixture = 'mixture'

class YesNoNullEnum(str, Enum):
    yes = 'yes'
    no = 'no'
    null = 'null'

class ResultCategory(str, Enum):
    kp = 'kp'
    hl = 'hl'
    qy = 'qy'
    k_OH = 'k_OH'
    k_1O2 = 'k_1O2'
    k_3DOM = 'k_3DOM'
    other = 'other'

class OriginEnum(str, Enum):
    current_study = 'current_study'
    cited_literature = 'cited_literature'

class ValueKindEnum(str, Enum):
    exact = 'exact'
    approx = 'approx'
    range = 'range'
    inequality = 'inequality'
    nd = 'nd'
    other_text = 'other_text'

def _clean_value_text(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, dict) and 'v' in x:
        x = x['v']
    s = str(x).strip()
    return s if s else None

def _is_pure_number_string(s: str) -> bool:
    s2 = s.replace(',', '').strip()
    return bool(re.fullmatch('[-+]?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?', s2))

def _infer_value_kind(text: str) -> ValueKindEnum:
    t = text.strip().lower()
    nd_markers = ['nd', 'not detected', 'non-detected', 'n.d.', 'below detection', 'below limit', '<lod', 'lod', '<loq', 'loq']
    for m in nd_markers:
        if m in t:
            return ValueKindEnum.nd
    if any((sym in t for sym in ['≤', '≥', '<', '>', '≲', '≳'])):
        return ValueKindEnum.inequality
    if '–' in t:
        if re.search('\\d\\s*–\\s*\\d', t):
            return ValueKindEnum.range
        return ValueKindEnum.other_text
    if re.search('\\d\\s*-\\s*\\d', t):
        return ValueKindEnum.range
    if any((w in t for w in [' between ', ' from ', ' to ', 'range'])):
        if re.search('(between|from).*\\d', t) and (' to ' in t or '-' in t or '–' in t):
            return ValueKindEnum.range
    approx_markers = ['~', '≈', 'approx', 'approximately', 'around', 'about', 'ca.', 'ca ', 'estimated', 'estim.']
    if any((m in t for m in approx_markers)):
        return ValueKindEnum.approx
    return ValueKindEnum.other_text

class ChemicalTypeObj(BaseConfigModel):
    v: ChemicalTypeEnum = Field(..., description='pure_substance/mixture')
    src: SrcType

class LightNumObj(BaseConfigModel):
    v: int = Field(..., description='Count.')
    src: SrcType

class Chemical(BaseConfigModel):
    name: Txt = Field(...)
    abbr: Optional[Txt] = Field(None)
    cas: Optional[Txt] = Field(None, validation_alias=AliasChoices('cas', 'CAS', 'cas_number', 'CAS_Number'), description='CAS Registry Number explicitly reported in the source text.')
    smiles: Optional[Txt] = Field(None, validation_alias=AliasChoices('smiles', 'SMILES', 'smi'), description='SMILES string explicitly reported in the source text.')
    type: Optional[ChemicalTypeObj] = Field(None)

class LightSource(BaseConfigModel):
    type: Optional[Txt] = Field(None, description='Lamp/source type category, such as xenon lamp, mercury lamp, UV lamp, or natural sunlight.')
    model: Optional[Txt] = Field(None, description='Specific manufacturer/instrument/model name or model number only; do not store lamp type, power, or intensity here.')
    wl_range: Optional[Txt] = Field(None)
    pwr: Optional[Num] = Field(None, description='Lamp or light source power.')
    num: Optional[LightNumObj] = Field(None)
    int: Optional[Num] = Field(None, description='Light intensity, irradiance, photon flux, or fluence rate.')
    filter_used: bool = Field(False, description='True only when the source text explicitly states that a filter was used.')
    filter_desc: Optional[Txt] = Field(None, description='Details of an explicitly used filter.')
    sunlight_desc: Optional[Txt] = Field(None)

class Reactor(BaseConfigModel):
    path: Optional[Num] = Field(None, description='Optical path length only; do not use reactor/container diameter.')
    dist: Optional[Num] = Field(None)
    mat: Optional[Txt] = Field(None)

class Water(BaseConfigModel):
    type: Optional[Txt] = Field(None)
    loc: Optional[Txt] = Field(None)
    deox: Optional[Annotated[YesNoNullEnum, BeforeValidator(normalize_deoxygenated)]] = Field(None)
    ox: Optional[Annotated[YesNoNullEnum, BeforeValidator(normalize_oxygenated)]] = Field(None)

class Setup(BaseConfigModel):
    chemical: Chemical = Field(...)
    light_source: LightSource = Field(...)
    reactor: Optional[Reactor] = Field(None)
    water: Optional[Water] = Field(None)

class PhObj(BaseConfigModel):
    v: Optional[RobustFloat] = Field(None)
    min: Optional[RobustFloat] = Field(None)
    max: Optional[RobustFloat] = Field(None)
    desc: Optional[RobustString] = Field(None)
    src: SrcType

class TempObj(BaseConfigModel):
    v: Optional[RobustFloat] = Field(None)
    u: Optional[RobustString] = Field(None)
    desc: Optional[RobustString] = Field(None)
    src: SrcType

class AdditiveObj(BaseConfigModel):
    id: str = Field(default_factory=generate_id, alias='_id')
    name: Txt = Field(..., description='Added substance, including DOM-related substances/categories when reported.')
    conc: Optional[Num] = Field(None)
    desc: Optional[RobustString] = Field(None)

class Conditions(BaseConfigModel):
    conc: Optional[Num] = Field(None)
    pH: Optional[PhObj] = Field(None, validation_alias=AliasChoices('pH', 'ph'))
    temp: Optional[TempObj] = Field(None)
    buffer_added: bool = Field(False)
    additives: Optional[List[AdditiveObj]] = Field(None)

class KineticResult(BaseConfigModel):
    cat: Annotated[ResultCategory, BeforeValidator(normalize_category_fuzzy)] = Field(..., description='Category: kp, hl, qy, other...')
    param_name: Optional[RobustString] = Field(None, description="Specific parameter name if cat is 'other'.")
    val: Optional[RobustFloat] = Field(None)
    val_text: Optional[RobustString] = Field(None)
    val_kind: ValueKindEnum = Field(ValueKindEnum.exact)
    e: int = Field(0)
    u: Optional[RobustString] = Field(None)
    sd: Optional[RobustFloat] = Field(None)
    specific_wl: Optional[Txt] = Field(None)
    origin: OriginEnum = Field(OriginEnum.current_study)
    src: SrcType

    @model_validator(mode='before')
    @classmethod
    def split_exact_and_inexact(cls, data: Any):
        if not isinstance(data, dict):
            return data
        if 'val_text' in data and data.get('val_text') is not None:
            vt = _clean_value_text(data.get('val_text'))
            if vt is None:
                data['val_text'] = None
            else:
                data['val_text'] = vt
                if 'val_kind' not in data or data.get('val_kind') in (None, '', 'null'):
                    data['val_kind'] = _infer_value_kind(vt)
        raw_val = data.get('val', None)
        if raw_val is None:
            return data
        if isinstance(raw_val, dict) and 'v' in raw_val:
            raw_val = raw_val.get('v')
        if isinstance(raw_val, (int, float)):
            if data.get('val_text') not in (None, '', 'null'):
                vk = data.get('val_kind', None)
                if vk not in (ValueKindEnum.exact, 'exact', None, '', 'null'):
                    data['val'] = None
            else:
                data['val_kind'] = ValueKindEnum.exact
            return data
        if isinstance(raw_val, str):
            s = _clean_value_text(raw_val)
            if s is None:
                data['val'] = None
                return data
            if _is_pure_number_string(s):
                data['val'] = s
                if data.get('val_text') in (None, '', 'null'):
                    data['val_kind'] = ValueKindEnum.exact
                return data
            data['val_text'] = s
            data['val'] = None
            data['val_kind'] = _infer_value_kind(s)
            return data
        s = _clean_value_text(raw_val)
        if s is None:
            data['val'] = None
            return data
        if _is_pure_number_string(s):
            data['val'] = s
            if data.get('val_text') in (None, '', 'null'):
                data['val_kind'] = ValueKindEnum.exact
            return data
        data['val_text'] = s
        data['val'] = None
        data['val_kind'] = _infer_value_kind(s)
        return data

    @model_validator(mode='after')
    def validate_exactness_consistency(self):
        if self.val is None and (self.val_text is None or str(self.val_text).strip() == ''):
            raise ValueError("KineticResult requires either 'val' (exact numeric) or 'val_text' (inexact/text).")
        if self.val is not None:
            self.val_kind = ValueKindEnum.exact
            if self.val_text is not None and str(self.val_text).strip() != '':
                self.val_text = None
        if self.val is None and self.val_text is not None and (str(self.val_text).strip() != ''):
            if self.val_kind == ValueKindEnum.exact:
                self.val_kind = _infer_value_kind(str(self.val_text))
        return self

    @model_validator(mode='after')
    def validate_other_category_logic(self):
        if self.cat == ResultCategory.other:
            if not self.param_name or not str(self.param_name).strip():
                raise ValueError("Field 'param_name' is required when category is 'other'.")
        elif self.param_name is not None:
            self.param_name = None
        return self

class DataPoint(BaseConfigModel):
    id: str = Field(default_factory=generate_id, alias='_id')
    result: KineticResult = Field(...)
    conditions: Optional[Conditions] = Field(None)
    special_note: Optional[RobustString] = Field(None, validation_alias=AliasChoices('special_note', 'special note', 'specialNote'), description='Optional scientific caveat or concern about this kinetic data point. Use only when the source value should still be extracted but its interpretation/calculation is scientifically problematic.')

class Experiment(BaseConfigModel):
    id: str = Field(default_factory=generate_id, alias='_id')
    setup: Setup = Field(...)
    data: List[DataPoint] = Field(...)

class ExtractionResult(BaseConfigModel):
    experiments: List[Experiment] = Field(...)
