import os
import json
import pandas as pd
import numpy as np
import requests
import re
import logging
import threading
import time
import sys
import tempfile
import unicodedata
from urllib.parse import quote
from enum import Enum
from typing import Dict, List, Any, Optional, Tuple, Set, Union, get_args, get_origin
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import warnings
from dotenv import load_dotenv
from pydantic import BaseModel
import tdm_logger
from pydantic_step2 import Setup, Conditions, KineticResult
from run_quant_summary import generate_run_quant_summary
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)
load_dotenv()
_SCRIPT_DIR_8 = os.path.dirname(os.path.abspath(__file__))
tdm_logger.setup_tdm_logging('Step8_Postprocess')
CONFIG = {'CHEMSPIDER_API_KEY': os.environ.get('CHEMSPIDER_API_KEY', ''), 'COMPTOX_API_KEY': os.environ.get('COMPTOX_API_KEY', ''), 'DATABASE_PRIORITY': ['PubChem', 'ChemSpider', 'CompTox', 'CACTUS', 'ChEMBL'], 'MAX_WORKERS': 5}
STEP3_SUCCESS_STATUSES = {'audited_clean', 'audited_patched'}
SEMANTIC_FAILURE_EXIT_CODE = 2
NULL_TEXT_VALUES = {'', 'nan', 'none', 'null'}
CONCENTRATION_UNIT_COLUMNS = {'conditions.conc.unit'}
KINETIC_RESULT_UNIT_COLUMNS = {'result.u'}
LIGHT_POWER_UNIT_COLUMNS = {'setup.light_source.pwr.unit'}
LIGHT_INTENSITY_UNIT_COLUMNS = {'setup.light_source.int.unit'}
LENGTH_UNIT_COLUMNS = {'setup.reactor.dist.unit', 'setup.reactor.path.unit'}
TEMPERATURE_UNIT_COLUMNS = {'conditions.temp.unit'}
SOURCE_CAS_VALUE_COL = 'setup.chemical.cas.value'
SOURCE_CAS_SRC_COL = 'setup.chemical.cas.src'
SOURCE_SMILES_VALUE_COL = 'setup.chemical.smiles.value'
SOURCE_SMILES_SRC_COL = 'setup.chemical.smiles.src'
INTERNAL_ONLY_COLUMNS = {SOURCE_CAS_VALUE_COL, SOURCE_CAS_SRC_COL, SOURCE_SMILES_VALUE_COL, SOURCE_SMILES_SRC_COL, 'SMILES_raw', 'InChIKey', 'PubChem_CID', 'Enrichment_Source'}
CAS_PATTERN = re.compile('\\b\\d{2,7}-\\d{2}-\\d\\b')
CONCENTRATION_CANONICAL_UNITS = {'M', 'mM', 'uM', 'nM', 'g L-1', 'mg L-1', 'ug L-1', 'ng L-1', 'ppm', 'ppb'}
MASS_CONCENTRATION_FACTORS = {'g L-1': 1.0, 'mg L-1': 0.001, 'ug L-1': 1e-06, 'ng L-1': 1e-09, 'ppm': 0.001, 'ppb': 1e-06}

def _is_missing_text(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except TypeError:
        pass
    return str(value).strip().lower() in NULL_TEXT_VALUES

def _clean_unit_text(raw_unit: Any) -> Optional[str]:
    if _is_missing_text(raw_unit):
        return None
    text = unicodedata.normalize('NFKC', str(raw_unit).strip())
    text = text.replace('μ', 'u').replace('µ', 'u')
    text = text.replace('−', '-').replace('–', '-').replace('—', '-')
    text = text.replace('⁻', '-').replace('¹', '1').replace('²', '2').replace('³', '3')
    text = text.replace('·', ' ').replace('•', ' ').replace('⋅', ' ').replace('×', ' ')
    text = text.replace('／', '/').replace('℃', '°C')
    text = re.sub('\\bper\\b', '/', text, flags=re.IGNORECASE)
    text = re.sub('\\blit(er|re)s?\\b', 'L', text, flags=re.IGNORECASE)
    text = text.replace('{', '').replace('}', '')
    text = re.sub('\\^\\s*\\(?\\s*([+-]?\\d+)\\s*\\)?', '\\1', text)
    text = re.sub('\\s*/\\s*', '/', text)
    text = re.sub('\\s+', ' ', text).strip()
    lowered = text.lower()
    if lowered in NULL_TEXT_VALUES:
        return None
    return lowered

def _compact_unit_text(raw_unit: Any) -> Optional[str]:
    cleaned = _clean_unit_text(raw_unit)
    if cleaned is None:
        return None
    compact = cleaned.replace(' ', '')
    compact = compact.replace('(', '').replace(')', '').replace('[', '').replace(']', '')
    compact = compact.replace('_', '')
    compact = compact.replace('/dm3', 'dm-3').replace('/dm-3', 'dm-3')
    compact = compact.replace('/ml', 'ml-1').replace('/l', 'l-1')
    compact = compact.replace('dm3', 'dm-3')
    compact = compact.replace('l^-1', 'l-1').replace('ml^-1', 'ml-1')
    compact = compact.replace('dm^-3', 'dm-3')
    return compact

def _normalize_concentration_unit(raw_unit: Any) -> Optional[str]:
    compact = _compact_unit_text(raw_unit)
    if compact is None:
        return None
    if compact in {'m', 'moll-1'}:
        return 'M'
    if compact in {'mm', 'mmoll-1'}:
        return 'mM'
    if compact in {'um', 'umoll-1'}:
        return 'uM'
    if compact in {'nm', 'nmoll-1'}:
        return 'nM'
    if compact in {'gl-1', 'gdm-3', 'mgml-1'}:
        return 'g L-1'
    if compact in {'mgl-1', 'mgdm-3', 'ugml-1'}:
        return 'mg L-1'
    if compact in {'ugl-1', 'ugdm-3', 'ngml-1'}:
        return 'ug L-1'
    if compact in {'ngl-1', 'ngdm-3'}:
        return 'ng L-1'
    if compact == 'ppm':
        return 'ppm'
    if compact == 'ppb':
        return 'ppb'
    return None

def _normalize_kinetic_result_unit(raw_unit: Any) -> Optional[str]:
    compact = _compact_unit_text(raw_unit)
    if compact is None:
        return None
    if compact in {'s', 'sec', 'second', 'seconds'}:
        return 's'
    if compact in {'min', 'minute', 'minutes'}:
        return 'min'
    if compact in {'h', 'hr', 'hrs', 'hour', 'hours'}:
        return 'h'
    if compact in {'d', 'day', 'days'}:
        return 'd'
    if compact in {'1/s', 's-1', 'sec-1', 'second-1', 'seconds-1'}:
        return 's-1'
    if compact in {'1/min', 'min-1', 'minute-1', 'minutes-1'}:
        return 'min-1'
    if compact in {'1/h', 'h-1', 'hr-1', 'hrs-1', 'hour-1', 'hours-1'}:
        return 'h-1'
    if compact in {'1/d', 'd-1', 'day-1', 'days-1'}:
        return 'd-1'
    if compact in {'m-1s-1', 'm^-1s^-1', 'l/mols', 'l/mol/s', 'lmol-1s-1', 'lmol^-1s^-1', 'mol-1ls-1'}:
        return 'M-1 s-1'
    if compact in {'cm2/j', 'cm2j-1', 'j-1cm2'}:
        return 'cm2 J-1'
    if compact in {'cm2mj-1', 'cm2/mj', 'mj-1cm2'}:
        return 'cm2 mJ-1'
    if compact in {'mol/einstein', 'moleinstein-1'}:
        return 'mol einstein-1'
    return None

def _normalize_light_power_unit(raw_unit: Any) -> Optional[str]:
    compact = _compact_unit_text(raw_unit)
    if compact is None:
        return None
    if compact in {'w', 'watt', 'watts'}:
        return 'W'
    if compact in {'mw', 'milliwatt', 'milliwatts'}:
        return 'mW'
    if compact in {'kw', 'kilowatt', 'kilowatts'}:
        return 'kW'
    return None

def _normalize_light_intensity_unit(raw_unit: Any) -> Optional[str]:
    compact = _compact_unit_text(raw_unit)
    if compact is None:
        return None
    if compact in {'wm-2', 'w/m2'}:
        return 'W m-2'
    if compact in {'mwm-2', 'mw/m2'}:
        return 'mW m-2'
    if compact in {'uwm-2', 'uw/m2'}:
        return 'uW m-2'
    if compact in {'wcm-2', 'w/cm2'}:
        return 'W cm-2'
    if compact in {'mwcm-2', 'mw/cm2'}:
        return 'mW cm-2'
    if compact in {'uwcm-2', 'uw/cm2'}:
        return 'uW cm-2'
    if compact in {'wm-3', 'w/m3'}:
        return 'W m-3'
    if compact in {'mwm-3', 'mw/m3'}:
        return 'mW m-3'
    if compact in {'einsteinl-1s-1', 'einstein/l/s', 'einsteinl-1sec-1'}:
        return 'Einstein L-1 s-1'
    if compact in {'photonss-1cm-2', 'photonscm-2s-1', 'photons/cm2/s', 'photons/(cm2s)', 'photonscm-2sec-1'}:
        return 'photons cm-2 s-1'
    return None

def _normalize_length_unit(raw_unit: Any) -> Optional[str]:
    compact = _compact_unit_text(raw_unit)
    if compact is None:
        return None
    if compact in {'m', 'cm', 'mm'}:
        return compact
    return None

def _normalize_temperature_unit(raw_unit: Any) -> Optional[str]:
    compact = _compact_unit_text(raw_unit)
    if compact is None:
        return None
    if compact in {'c', '°c', 'degc', 'celsius'}:
        return '°C'
    return None

def _infer_unit_context(column_name: str) -> Optional[str]:
    if column_name in CONCENTRATION_UNIT_COLUMNS:
        return 'concentration'
    if column_name in KINETIC_RESULT_UNIT_COLUMNS:
        return 'kinetic_result'
    if column_name in LIGHT_POWER_UNIT_COLUMNS:
        return 'light_power'
    if column_name in LIGHT_INTENSITY_UNIT_COLUMNS:
        return 'light_intensity'
    if column_name in LENGTH_UNIT_COLUMNS:
        return 'length'
    if column_name in TEMPERATURE_UNIT_COLUMNS:
        return 'temperature'
    return None

def _normalize_unit_for_context(raw_unit: Any, context: str) -> Any:
    if _is_missing_text(raw_unit):
        return raw_unit
    if context == 'concentration':
        normalized = _normalize_concentration_unit(raw_unit)
    elif context == 'kinetic_result':
        normalized = _normalize_kinetic_result_unit(raw_unit)
    elif context == 'light_power':
        normalized = _normalize_light_power_unit(raw_unit)
    elif context == 'light_intensity':
        normalized = _normalize_light_intensity_unit(raw_unit)
    elif context == 'length':
        normalized = _normalize_length_unit(raw_unit)
    elif context == 'temperature':
        normalized = _normalize_temperature_unit(raw_unit)
    else:
        normalized = None
    return normalized if normalized is not None else raw_unit

def _safe_float(value: Any) -> Optional[float]:
    if _is_missing_text(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _looks_like_smiles(value: Any) -> bool:
    if _is_missing_text(value):
        return False
    text = str(value).strip()
    if not text or any((ch.isspace() for ch in text)):
        return False
    if CAS_PATTERN.fullmatch(text):
        return False
    return bool(re.search('[=#@\\[\\]\\(\\)\\\\/]', text)) or bool(re.search('\\b(Cl|Br|Si|Na|Li|Mg|Ca)\\b', text))

def _clean_chemical_name_for_query(value: Any, abbr: Any=None) -> Optional[str]:
    if _is_missing_text(value):
        return None
    text = str(value).strip()
    abbr_text = None if _is_missing_text(abbr) else str(abbr).strip()
    if abbr_text:
        text = re.sub(f'\\s*\\(\\s*{re.escape(abbr_text)}\\s*\\)\\s*$', '', text, flags=re.IGNORECASE).strip()
    return text or None

def _is_valid_cas(value: str) -> bool:
    if not CAS_PATTERN.fullmatch(value):
        return False
    digits = value.replace('-', '')
    check = int(digits[-1])
    total = sum((int(digit) * weight for weight, digit in enumerate(reversed(digits[:-1]), start=1)))
    return total % 10 == check

def _is_internal_output_column(column_name: str) -> bool:
    return column_name in INTERNAL_ONLY_COLUMNS or column_name.startswith('conditions.dom_')
logger = logging.getLogger()

class ChemicalDataScraper:

    def __init__(self, cache_file: str=None):
        self._session_headers = {'User-Agent': 'Scientific-TDM-Bot/2.0 (Research Purpose)'}
        self._session_local = threading.local()
        self.cache_file = cache_file or os.path.join(_SCRIPT_DIR_8, 'scraper_cache.json')
        self._cache_lock = threading.Lock()
        self.cache = self._load_cache()

    def _get_session(self) -> requests.Session:
        session = getattr(self._session_local, 'session', None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._session_headers)
            self._session_local.session = session
        return session

    def _load_cache(self) -> Dict:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f'Could not load cache file: {e}. Starting with empty cache.')
                return {}
        return {}

    def save_cache(self):
        with self._cache_lock:
            cache_dir = os.path.dirname(self.cache_file) or '.'
            os.makedirs(cache_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix='scraper_cache_', suffix='.tmp', dir=cache_dir)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(self.cache, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.cache_file)
            except Exception as e:
                logger.error(f'Failed to save cache: {e}')
                try:
                    os.close(fd)
                except OSError:
                    pass
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    def _validate_input(self, text: Any) -> Optional[str]:
        if pd.isna(text) or str(text).strip() == '' or str(text).lower() in ['nan', 'none', 'null']:
            return None
        return str(text).strip()

    def get_chemical_info(self, identifier: str, auto_save: bool=True) -> Dict[str, Any]:
        clean_id = self._validate_input(identifier)
        if not clean_id:
            return {}
        with self._cache_lock:
            if clean_id in self.cache:
                cached = self.cache[clean_id]
                if isinstance(cached, dict):
                    cached.setdefault('SMILES_raw', cached.get('SMILES'))
                    cached.setdefault('IsomericSMILES', None)
                    cached.setdefault('InChIKey', None)
                    cached.setdefault('PubChem_CID', None)
                    cached.setdefault('Enrichment_Source', 'cache')
                return cached
        result = {'SMILES': None, 'SMILES_raw': None, 'IsomericSMILES': None, 'Molecular Weight': None, 'IUPAC_Name': None, 'CAS_Number': None, 'InChIKey': None, 'PubChem_CID': None, 'Enrichment_Source': None}
        for db in CONFIG['DATABASE_PRIORITY']:
            try:
                if db == 'PubChem':
                    res = self._query_pubchem(clean_id)
                elif db == 'ChemSpider':
                    res = self._query_chemspider(clean_id)
                elif db == 'CompTox':
                    res = self._query_comptox(clean_id)
                elif db == 'CACTUS':
                    res = self._query_cactus(clean_id)
                elif db == 'ChEMBL':
                    res = self._query_chembl(clean_id)
                else:
                    res = {}
                for k, v in res.items():
                    if not result.get(k) and v:
                        result[k] = v
                if result['Molecular Weight'] and result['SMILES'] and result.get('InChIKey'):
                    break
            except Exception as e:
                logger.debug(f'Error querying {db} for {clean_id}: {e}')
        with self._cache_lock:
            if clean_id in self.cache:
                return self.cache[clean_id]
            self.cache[clean_id] = result
        if auto_save:
            self.save_cache()
        return result

    def _query_pubchem_property(self, namespace: str, identifier: str) -> Dict:
        properties = 'CanonicalSMILES,IsomericSMILES,InChIKey,IUPACName,MolecularWeight'
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/{namespace}/{quote(identifier, safe='')}/property/{properties}/JSON"
        try:
            resp = self._get_session().get(url, timeout=10)
            if resp.status_code == 200:
                props = resp.json()['PropertyTable']['Properties'][0]
                cid = props.get('CID')
                cas_number = self._query_pubchem_cas(cid) if cid else None
                return {'SMILES': props.get('CanonicalSMILES'), 'SMILES_raw': props.get('CanonicalSMILES'), 'IsomericSMILES': props.get('IsomericSMILES'), 'Molecular Weight': props.get('MolecularWeight'), 'IUPAC_Name': props.get('IUPACName'), 'CAS_Number': cas_number, 'InChIKey': props.get('InChIKey'), 'PubChem_CID': cid, 'Enrichment_Source': f'PubChem:{namespace}'}
        except:
            pass
        return {}

    def _query_pubchem_cas(self, cid: Any) -> Optional[str]:
        try:
            resp = self._get_session().get(f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/synonyms/JSON', timeout=10)
            if resp.status_code != 200:
                return None
            synonyms = resp.json().get('InformationList', {}).get('Information', [{}])[0].get('Synonym', [])
            for synonym in synonyms:
                match = CAS_PATTERN.fullmatch(str(synonym).strip())
                if match and _is_valid_cas(match.group(0)):
                    return match.group(0)
        except:
            pass
        return None

    def _query_pubchem(self, identifier: str) -> Dict:
        result = self._query_pubchem_property('name', identifier)
        if result:
            return result
        if _looks_like_smiles(identifier):
            return self._query_pubchem_property('smiles', identifier)
        return {}

    def _query_chemspider(self, identifier: str) -> Dict:
        if not CONFIG['CHEMSPIDER_API_KEY']:
            return {}
        return {}

    def _query_cactus(self, identifier: str) -> Dict:
        base = 'https://cactus.nci.nih.gov/chemical/structure'
        res = {}
        try:
            session = self._get_session()
            smiles_resp = session.get(f'{base}/{quote(identifier)}/smiles', timeout=10)
            if smiles_resp.status_code == 200:
                res['SMILES'] = smiles_resp.text.strip()
                res['SMILES_raw'] = smiles_resp.text.strip()
            mw_resp = session.get(f'{base}/{quote(identifier)}/mw', timeout=10)
            if mw_resp.status_code == 200:
                res['Molecular Weight'] = mw_resp.text.strip()
            cas_resp = session.get(f'{base}/{quote(identifier)}/cas', timeout=10)
            if cas_resp.status_code == 200:
                res['CAS_Number'] = cas_resp.text.strip().split('\n')[0]
            if res:
                res['Enrichment_Source'] = 'CACTUS'
        except:
            pass
        return res

    def _query_comptox(self, identifier: str) -> Dict:
        return {}

    def _query_chembl(self, identifier: str) -> Dict:
        return {}

class IntegratedTDMPipeline:

    def __init__(self, input_dir: str, output_dir: str, template_path: str=None):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.scraper = ChemicalDataScraper()
        self.expected_output_columns = self._order_columns(self._build_expected_output_columns())
        os.makedirs(output_dir, exist_ok=True)

    def run(self):
        logger.info(f'🚀 Starting Pipeline on folder: {self.input_dir}')
        start_time = time.time()
        timestamp = int(time.time())
        df_raw = self._step_json_to_dataframe()
        if df_raw.empty:
            output_path = self._save_empty_outputs(timestamp)
            duration = time.time() - start_time
            self._write_run_quant_summary(timestamp, output_path, 0, duration)
            logger.info(f'✅ Pipeline Completed in {duration:.2f}s with no extractable rows.')
            return
        df_clean = self._step_preprocessing(df_raw)
        df_enriched = self._step_enrichment(df_clean)
        df_final = self._step_postprocessing(df_enriched)
        output_path = os.path.join(self.output_dir, f'TDM_Final_Database_{timestamp}.xlsx')
        df_final.to_excel(output_path, index=False)
        self._write_run_quant_summary(timestamp, output_path, len(df_final), time.time() - start_time)
        logger.info(f'✅ Full Database Saved: {output_path}')
        duration = time.time() - start_time
        logger.info(f'✅ Pipeline Completed in {duration:.2f}s.')

    def _save_empty_outputs(self, timestamp: int):
        logger.info('No extractable rows found in audited JSON files. Writing empty final database.')
        output_path = os.path.join(self.output_dir, f'TDM_Final_Database_{timestamp}.xlsx')
        pd.DataFrame(columns=self.expected_output_columns).to_excel(output_path, index=False)
        logger.info(f'✅ Empty Full Database Saved: {output_path}')
        return output_path

    def _write_run_quant_summary(self, timestamp: int, final_database_path: str, final_row_count: int, step8_duration_s: float) -> None:
        run_dir = os.path.abspath(os.path.join(self.output_dir, os.pardir))
        try:
            summary_path = generate_run_quant_summary(run_dir=run_dir, output_dir=self.output_dir, timestamp=timestamp, final_database_path=final_database_path, final_database_rows=final_row_count, step8_duration_s=step8_duration_s)
            logger.info(f'Run quantitative summary saved: {summary_path}')
        except Exception as exc:
            logger.warning(f'Failed to generate run quantitative summary: {exc}', exc_info=True)

    def _step_json_to_dataframe(self) -> pd.DataFrame:
        json_files = [f for f in os.listdir(self.input_dir) if f.endswith('.json')]
        logger.info(f'Found {len(json_files)} JSON files.')
        all_rows = []
        for fname in json_files:
            fpath = os.path.join(self.input_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                metadata = data.get('metadata') or {}
                extraction = data.get('extraction') or {}
                experiments = extraction.get('experiments') or []
                if not experiments:
                    continue
                for exp in experiments:
                    if not exp:
                        continue
                    setup_flat = self._flatten_object(exp.get('setup') or {}, parent_key='setup')
                    data_points = exp.get('data') or []
                    for dp in data_points:
                        if not dp:
                            continue
                        row = {'source_file': fname, 'step3_quality_score': metadata.get('quality_score'), 'step3_status': metadata.get('step3_status'), 'doi': str(metadata.get('file', '')).replace('.json', '').replace('.md', ''), 'special_note': dp.get('special_note')}
                        row.update(setup_flat)
                        conditions_flat = self._flatten_object(dp.get('conditions') or {}, parent_key='conditions')
                        row.update(conditions_flat)
                        result_flat = self._flatten_object(dp.get('result') or {}, parent_key='result')
                        row.update(result_flat)
                        all_rows.append(row)
            except Exception as e:
                logger.error(f'Failed to process {fname}: {str(e)}')
        return pd.DataFrame(all_rows)

    def _unwrap_annotation(self, annotation: Any) -> Any:
        while True:
            origin = get_origin(annotation)
            if origin is Union:
                args = [arg for arg in get_args(annotation) if arg is not type(None)]
                if len(args) == 1:
                    annotation = args[0]
                    continue
            if getattr(annotation, '__metadata__', None) is not None:
                annotation = get_args(annotation)[0]
                continue
            return annotation

    def _build_placeholder_value(self, annotation: Any) -> Any:
        annotation = self._unwrap_annotation(annotation)
        origin = get_origin(annotation)
        if origin in (list, List):
            item_annotation = get_args(annotation)[0] if get_args(annotation) else Any
            return [self._build_placeholder_value(item_annotation)]
        if isinstance(annotation, type):
            if issubclass(annotation, BaseModel):
                payload = {}
                for field_name, field_info in annotation.model_fields.items():
                    payload[field_info.alias or field_name] = self._build_placeholder_value(field_info.annotation)
                return payload
            if issubclass(annotation, Enum):
                first_member = next(iter(annotation), None)
                return first_member.value if first_member is not None else None
            if annotation is bool:
                return False
            if annotation in (int, float):
                return 1
            if annotation is str:
                return 'placeholder'
        return 'placeholder'

    def _build_expected_output_columns(self) -> List[str]:
        metadata_cols = ['source_file', 'step3_quality_score', 'step3_status', 'doi']
        flattened_cols: List[str] = []
        for parent_key, model_cls in (('setup', Setup), ('conditions', Conditions), ('result', KineticResult)):
            placeholder_payload = self._build_placeholder_value(model_cls)
            flattened_cols.extend(self._flatten_object(placeholder_payload, parent_key).keys())
        flattened_cols = [col for col in flattened_cols if not _is_internal_output_column(col)]
        computed_cols = ['SMILES', 'Molecular Weight', 'CAS_Number', 'conditions.conc.normalized_M']
        value_scientific_cols = [col.replace('.value', '.value_scientific') for col in flattened_cols if col.endswith('.value') and col.replace('.value', '.exponent') in flattened_cols]
        return list(dict.fromkeys(metadata_cols + flattened_cols + value_scientific_cols + computed_cols + ['special_note']))

    def _order_columns(self, columns: List[str]) -> List[str]:
        priority_order = ['source_file', 'step3_quality_score', 'doi', 'setup.chemical', 'SMILES', 'CAS_Number', 'Molecular Weight', 'setup.light_source', 'setup.reactor', 'setup.water', 'conditions', 'result']
        remaining_cols = list(columns)
        ordered_cols = []
        for prefix in priority_order:
            matches = [col for col in remaining_cols if prefix in col]
            matches.sort()
            ordered_cols.extend(matches)
            remaining_cols = [col for col in remaining_cols if col not in matches]
        ordered_cols.extend(remaining_cols)
        return ordered_cols

    def _align_output_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        extra_cols = [col for col in df.columns if col not in self.expected_output_columns and (not _is_internal_output_column(col))]
        ordered_cols = self.expected_output_columns + self._order_columns(extra_cols)
        return df.reindex(columns=ordered_cols)

    def _flatten_object(self, obj: Dict, parent_key: str='') -> Dict:
        items: Dict[str, Any] = {}
        if obj is None:
            return items
        for k, v in obj.items():
            new_key = f'{parent_key}.{k}' if parent_key else k
            if isinstance(v, dict):
                if any((x in v for x in ['v', 'val', 'u', 'src', 'e'])):
                    self._extract_leaf_values(v, new_key, items)
                else:
                    items.update(self._flatten_object(v, new_key))
            elif isinstance(v, list):
                if not v:
                    continue
                if isinstance(v[0], dict):
                    serialized_list = []
                    for item in v:
                        name_obj = item.get('name') or {}
                        val = name_obj.get('v')
                        if not val:
                            val = json.dumps(item)
                        conc_obj = item.get('conc') or {}
                        conc = conc_obj.get('v')
                        unit = conc_obj.get('u')
                        desc = item.get('desc')
                        parts = [str(val)]
                        if conc:
                            parts.append(f"({conc} {unit or ''})")
                        if desc:
                            parts.append(f'[{desc}]')
                        serialized_list.append(' '.join(parts))
                    items[new_key] = '; '.join(serialized_list)
                else:
                    items[new_key] = '; '.join(map(str, v))
            else:
                items[new_key] = v
        return items

    def _extract_leaf_values(self, leaf_obj: Dict, prefix: str, items: Dict):
        val = leaf_obj.get('v')
        if val is None:
            val = leaf_obj.get('val')
        items[f'{prefix}.value'] = val
        if 'u' in leaf_obj:
            items[f'{prefix}.unit'] = leaf_obj['u']
        if 'e' in leaf_obj:
            items[f'{prefix}.exponent'] = leaf_obj['e']
        if 'src' in leaf_obj:
            srcs = leaf_obj['src']
            if isinstance(srcs, list):
                items[f'{prefix}.src'] = str(srcs)
            else:
                items[f'{prefix}.src'] = str(srcs)
        if 'specific_wl' in leaf_obj and isinstance(leaf_obj['specific_wl'], dict):
            items[f'{prefix}.specific_wl'] = leaf_obj['specific_wl'].get('v')

    def _step_preprocessing(self, df: pd.DataFrame) -> pd.DataFrame:
        logger.info('Processing scientific data (Units, Scientific Notation)...')
        preserve_case_tokens = ('chemical.name', 'chemical.abbr', 'chemical.cas', 'conditions.additives', 'source_file', 'param_name', 'special_note')
        for col in df.select_dtypes(include='object').columns:
            if not any((token in col for token in preserve_case_tokens)):
                df[col] = df[col].apply(lambda x: str(x).lower() if pd.notna(x) else x)
        unit_cols = [c for c in df.columns if c.endswith('.unit') or c == 'result.u']
        for col in unit_cols:
            context = _infer_unit_context(col)
            if context is None:
                continue
            df[col] = df[col].apply(lambda x, unit_context=context: _normalize_unit_for_context(x, unit_context))
        val_cols = [c for c in df.columns if c.endswith('.value') and c.replace('.value', '.exponent') in df.columns]
        for v_col in val_cols:
            e_col = v_col.replace('.value', '.exponent')
            new_col = v_col.replace('.value', '.value_scientific')
            df[v_col] = pd.to_numeric(df[v_col], errors='coerce')
            df[e_col] = pd.to_numeric(df[e_col], errors='coerce').fillna(0)
            df[new_col] = df[v_col] * 10.0 ** df[e_col]
        return df

    def _step_enrichment(self, df: pd.DataFrame) -> pd.DataFrame:
        logger.info('Enriching with external chemical databases...')
        name_col = 'setup.chemical.name.value'
        abbr_col = 'setup.chemical.abbr.value'
        cas_col = SOURCE_CAS_VALUE_COL
        raw_smiles_col = SOURCE_SMILES_VALUE_COL
        if name_col not in df.columns:
            logger.warning('Chemical name column not found. Skipping enrichment.')
            return df
        if abbr_col not in df.columns:
            df[abbr_col] = None
        if cas_col not in df.columns:
            df[cas_col] = None
        if raw_smiles_col not in df.columns:
            df[raw_smiles_col] = None
        df['SMILES'] = None
        df['SMILES_raw'] = None
        df['Molecular Weight'] = None
        df['CAS_Number'] = None
        df['InChIKey'] = None
        df['PubChem_CID'] = None
        df['Enrichment_Source'] = None
        unique_chems = df[[name_col, abbr_col, cas_col, raw_smiles_col]].drop_duplicates()
        chem_map = {}
        query_records = []

        def ordered_queries(source_cas: Optional[str], raw_smiles: Optional[str], name: Optional[str], abbr: Optional[str]) -> List[str]:
            return list(dict.fromkeys((q for q in [source_cas, raw_smiles, name, abbr] if q)))

        def has_external_info(info: Optional[Dict[str, Any]]) -> bool:
            if not info:
                return False
            return any((info.get(k) for k in ('SMILES', 'Molecular Weight', 'CAS_Number', 'IUPAC_Name')))

        def fetch_queries(queries: List[str], phase: str):
            to_scrape = []
            for query in list(dict.fromkeys(queries)):
                if query in chem_map:
                    continue
                if query in self.scraper.cache:
                    chem_map[query] = self.scraper.cache[query]
                else:
                    to_scrape.append(query)
            if not to_scrape:
                logger.info(f'All {phase} enrichment queries found in local cache.')
                return
            logger.info(f"Parallel scraping needed for {len(to_scrape)} {phase} enrichment queries with {CONFIG['MAX_WORKERS']} workers.")
            with ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as executor:
                future_to_chem = {executor.submit(self.scraper.get_chemical_info, q, auto_save=False): q for q in to_scrape}
                for future in tqdm(as_completed(future_to_chem), total=len(to_scrape), desc=f'Fetching APIs ({phase})'):
                    chem = future_to_chem[future]
                    try:
                        chem_map[chem] = future.result()
                    except Exception as e:
                        logger.error(f'Scraping failed for {chem}: {e}')
            self.scraper.save_cache()
        for _, row in unique_chems.iterrows():
            source_cas = self.scraper._validate_input(row.get(cas_col))
            raw_smiles = self.scraper._validate_input(row.get(raw_smiles_col))
            name = self.scraper._validate_input(_clean_chemical_name_for_query(row.get(name_col), row.get(abbr_col)))
            abbr = self.scraper._validate_input(row.get(abbr_col))
            queries = ordered_queries(source_cas, raw_smiles, name, abbr)
            if queries:
                query_records.append({'source_cas': source_cas, 'raw_smiles': raw_smiles, 'name': name, 'abbr': abbr, 'queries': queries})
        fetch_queries([record['queries'][0] for record in query_records], 'primary CAS/name')
        fallback_queries = []
        for record in query_records:
            primary_info = chem_map.get(record['queries'][0])
            if has_external_info(primary_info):
                continue
            fallback_queries.extend(record['queries'][1:])
        if fallback_queries:
            fetch_queries(fallback_queries, 'fallback name/abbr')

        def apply_map(row):
            source_cas = self.scraper._validate_input(row.get(cas_col))
            raw_smiles = self.scraper._validate_input(row.get(raw_smiles_col))
            name = self.scraper._validate_input(_clean_chemical_name_for_query(row.get(name_col), row.get(abbr_col)))
            abbr = self.scraper._validate_input(row.get(abbr_col))
            queries = ordered_queries(source_cas, raw_smiles, name, abbr)
            info = {}
            for query in queries:
                candidate = chem_map.get(query) or self.scraper.cache.get(query)
                if has_external_info(candidate):
                    info = candidate
                    break
            if info:
                return pd.Series([info.get('SMILES'), raw_smiles or info.get('SMILES_raw') or info.get('IsomericSMILES'), info.get('Molecular Weight'), source_cas or info.get('CAS_Number'), info.get('InChIKey'), info.get('PubChem_CID'), info.get('Enrichment_Source')])
            return pd.Series([raw_smiles, raw_smiles, None, source_cas, None, None, 'source_reported_smiles_fallback' if raw_smiles else None])
        df[['SMILES', 'SMILES_raw', 'Molecular Weight', 'CAS_Number', 'InChIKey', 'PubChem_CID', 'Enrichment_Source']] = df.apply(apply_map, axis=1)
        return df

    def _step_postprocessing(self, df: pd.DataFrame) -> pd.DataFrame:
        logger.info('Finalizing calculations and column ordering...')
        conc_val_col = 'conditions.conc.value_scientific'
        conc_unit_col = 'conditions.conc.unit'
        mw_col = 'Molecular Weight'
        target_col = 'conditions.conc.normalized_M'
        if conc_val_col in df.columns and mw_col in df.columns and (conc_unit_col in df.columns):
            df[target_col] = np.nan

            def calc_molarity(row):
                val = _safe_float(row.get(conc_val_col))
                mw = _safe_float(row.get(mw_col))
                unit = _normalize_concentration_unit(row.get(conc_unit_col))
                if val is None or mw is None or unit is None:
                    return None
                if unit == 'M':
                    return val
                if unit == 'mM':
                    return val * 0.001
                if unit == 'uM':
                    return val * 1e-06
                if unit == 'nM':
                    return val * 1e-09
                if unit in MASS_CONCENTRATION_FACTORS:
                    return val * MASS_CONCENTRATION_FACTORS[unit] / mw
                return None
            df[target_col] = df.apply(calc_molarity, axis=1)
            diagnostic_mask = df[conc_val_col].notna() & df[mw_col].notna() & df[target_col].isna()
            if diagnostic_mask.any():
                diagnostics = df.loc[diagnostic_mask, ['source_file', conc_unit_col, conc_val_col, mw_col]].copy()
                diagnostics['normalized_unit'] = diagnostics[conc_unit_col].apply(_normalize_concentration_unit)

                def classify_reason(row):
                    raw_unit = row.get(conc_unit_col)
                    if _is_missing_text(raw_unit):
                        return 'missing_concentration_unit'
                    if row.get('normalized_unit') is None:
                        return 'unknown_concentration_unit'
                    if row.get('normalized_unit') not in CONCENTRATION_CANONICAL_UNITS:
                        return 'unsupported_unit_for_molarity'
                    return 'conversion_failed'
                diagnostics['reason'] = diagnostics.apply(classify_reason, axis=1)
                summary_pairs = diagnostics.groupby(['reason', conc_unit_col]).size().items()
                summary_text = '; '.join((f'{reason} | {unit}: {count}' for (reason, unit), count in summary_pairs))
                logger.warning('Could not compute %s for %d rows with concentration values and molecular weights. %s', target_col, int(diagnostic_mask.sum()), summary_text or 'No diagnostic summary available.')
        internal_cols = [col for col in df.columns if _is_internal_output_column(col)]
        if internal_cols:
            df = df.drop(columns=internal_cols)
        return self._align_output_columns(df)
if __name__ == '__main__':
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
    _DATA_DIR = os.environ.get('TDM_DATA_DIR') or os.path.join(_PROJECT_ROOT, 'data')
    INPUT_FOLDER = os.path.join(_DATA_DIR, '07_audited_db')
    OUTPUT_FOLDER = os.path.join(_DATA_DIR, '08_final_excel')
    print('=' * 60)
    print('  TDM INTEGRATED POST-PROCESSING PIPELINE (Multithreaded)')
    print('  Target: Convert Step 3 JSONs -> Enriched Excel Database')
    print('=' * 60)
    if not os.path.exists(INPUT_FOLDER):
        print(f"Error: Input folder '{INPUT_FOLDER}' does not exist.")
        exit(1)
    pipeline = IntegratedTDMPipeline(INPUT_FOLDER, OUTPUT_FOLDER)
    pipeline.run()
