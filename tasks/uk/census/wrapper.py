from collections import OrderedDict, defaultdict

from luigi import WrapperTask, Parameter

from tasks.meta import current_session, GEOM_REF
from lib.timespan import get_timespan
from tasks.base_tasks import TableTask, InterpolationTask, CoupledInterpolationTask
from tasks.uk.cdrc import OutputAreas, OutputAreaColumns
from tasks.uk.census.metadata import CensusColumns
from tasks.uk.datashare import PostcodeAreas, PostcodeAreasColumns
from tasks.uk.odl import PostcodeDistricts, PostcodeDistrictsColumns, PostcodeSectors, PostcodeSectorsColumns
from tasks.uk.gov import (LowerLayerSuperOutputAreas, LowerLayerSuperOutputAreasColumns,
                          MiddleLayerSuperOutputAreas, MiddleLayerSuperOutputAreasColumns)

from lib.logger import get_logger

from .ons import ImportUKOutputAreas, ImportEnglandWalesLocal
from .scotland import ImportScotland
from .metadata import parse_table, COLUMNS_DEFINITION

LOGGER = get_logger(__name__)


class CensusOutputAreasTableTask(WrapperTask):
    # Which task to use to import an specific table
    REGION_MAPPING = {
        "UK": ImportUKOutputAreas,
        "EW": ImportEnglandWalesLocal,
        "SC": ImportScotland
    }

    table = Parameter()

    def requires(self):
        _, region = parse_table(self.table)
        return self.REGION_MAPPING[region](table=self.table)

    def id_to_column(self, column_id):
        return self.requires().id_to_column(column_id)

    def output_table(self):
        return self.requires().output().table


class CensusOutputAreas(TableTask):
    def requires(self):
        deps = {
            'geom_columns': OutputAreaColumns(),
            'data_columns': CensusColumns(),
            'geo': OutputAreas(),
        }
        for t in self.source_tables():
            deps[t] = CensusOutputAreasTableTask(table=t)

        return deps

    def targets(self):
        return {
            self.input()['geo'].obs_table: GEOM_REF,
        }

    def table_timespan(self):
        return get_timespan('2011')

    def columns(self):
        cols = OrderedDict()
        input_ = self.input()
        cols['GeographyCode'] = input_['geom_columns']['oa_sa']
        cols.update(input_['data_columns'])
        return cols

    def source_tables(self):
        tables = set()
        for col in COLUMNS_DEFINITION.values():
            tables.update([d['table'] for d in col['data']])
        return tables

    def populate(self):
        '''
        Joins data from all UK sub-census dependencies.

        For each column, there are multiple possiblities:
          - The column has a single datasource and field: ``"data" : [{"table": "t", "fields": ["f"]}]``.
            The table will be added to the FROM, and the column to the SELECT
            ``SELECT t.f AS out_name FROM ... FULL JOIN t USING (geographycode)``

          - The column has multiple fields: ``"data" : [{"table": "t", "fields": ["f1", "f2"]}]``.
            The table will be added to the FROM, and an expression adding all columns to the SELECT
            ``SELECT t.f1 + t.f2 AS out_name FROM ... FULL JOIN t USING (geographycode)``

          - The column has multiple tables: ``"data" : [{"table": "t1", "fields": ["f"]}, {"table": "t2", "fields": ["f"]}]``.
            The tables will be added to a CTE that UNIONS them, the CTE to the FROM, and the column to the SELECT
            ``WITH t1t2 AS (SELECT f AS out_name FROM t1 UNION SELECT f as out_name FROM t2)
              SELECT t1t2.out_name FROM ... FULL JOIN t1t2 USING (geographycode)``

          - The combination of both, which works as above, putting the summing expression inside the CTE
        '''
        def table_column_expression(data):
            tabletask = self.requires()[data['table']]
            table = tabletask.output_table()
            column_names = [tabletask.id_to_column(f) for f in data['fields']]
            col_expression = '+'.join(['{table}.{column}'.format(table=table, column=cn) for cn in column_names])

            return table, col_expression

        in_colnames = []  # Column names in source tables
        out_colnames = []  # Column names in destination table
        from_tables = set()  # Set of all source tables / CTEs (FROM generation)
        ctes = defaultdict(lambda: defaultdict(list))  # CTEs to be generated. {t1t2: { t1: [c1, c2], t2: [d1, d2]}}

        # Generate SQL parts for each column
        for k, v in COLUMNS_DEFINITION.items():
            data = v['data']
            if len(data) == 1:
                # Single table source
                table, col_expression = table_column_expression(data[0])

                from_tables.add(table)
                in_colnames.append(col_expression)
                out_colnames.append(k)
            else:
                # Multi data source
                cte_name = '_'.join([d['table'] for d in data]).lower()
                cte = ctes[cte_name]
                for d in data:
                    table, col_expression = table_column_expression(d)
                    cte[table].append('{expression} AS {id}'.format(expression=col_expression, id=k))

                in_colnames.append('{cte_name}.{id}'.format(cte_name=cte_name, id=k))
                out_colnames.append(k)

        # Generate SQL for CTEs
        ctes_sql = []
        for name, cte in ctes.items():
            selects = []
            for table, columns in cte.items():
                selects.append('SELECT geographycode, {cols} FROM {table}'.format(cols=', '.join(columns), table=table))
            ctes_sql.append('{name} AS ({union})'.format(name=name, union=' UNION '.join(selects)))
            from_tables.add(name)

        # Generate FROM clause. Uses FULL JOIN because not all tables are complete.
        tables = list(from_tables)
        from_part = tables[0]
        for t in tables[1:]:
            from_part += ' FULL JOIN {} USING (geographycode)'.format(t)

        stmt = 'WITH {ctes} INSERT INTO {output} (geographycode, {out_colnames}) ' \
               'SELECT geographycode, {in_colnames} ' \
               'FROM {from_part} ' \
               'WHERE geographycode LIKE \'_00%\''.format(
                   ctes=', '.join(ctes_sql),
                   output=self.output().table,
                   out_colnames=', '.join(out_colnames),
                   in_colnames=', '.join(in_colnames),
                   from_part=from_part)

        current_session().execute(stmt)


class CensusPostcodeAreas(InterpolationTask):
    def table_timespan(self):
        return get_timespan('2011')

    def columns(self):
        cols = OrderedDict()
        input_ = self.input()
        cols['pa_id'] = input_['target_geom_columns']['pa_id']
        cols.update(input_['source_data_columns'])
        return cols

    def requires(self):
        deps = {
            'source_geom_columns': OutputAreaColumns(),
            'source_geom': OutputAreas(),
            'source_data_columns': CensusColumns(),
            'source_data': CensusOutputAreas(),
            'target_geom_columns': PostcodeAreasColumns(),
            'target_geom': PostcodeAreas(),
            'target_data_columns': CensusColumns(),
        }

        return deps

    def get_interpolation_parameters(self):
        params = {
            'source_data_geoid': 'geographycode',
            'source_geom_geoid': 'oa_sa',
            'target_data_geoid': 'pa_id',
            'target_geom_geoid': 'pa_id',
            'source_geom_geomfield': 'the_geom',
            'target_geom_geomfield': 'the_geom',
        }

        return params


class CensusPostcodeEntitiesFromOAs(InterpolationTask):
    def table_timespan(self):
        return get_timespan('2011')

    def columns(self):
        cols = OrderedDict()
        input_ = self.input()
        cols['GeographyCode'] = input_['target_geom_columns']['geographycode']
        cols.update(input_['target_data_columns'])
        return cols

    def get_interpolation_parameters(self):
        params = {
            'source_data_geoid': 'geographycode',
            'source_geom_geoid': 'oa_sa',
            'target_data_geoid': 'geographycode',
            'target_geom_geoid': 'geographycode',
            'source_geom_geomfield': 'the_geom',
            'target_geom_geomfield': 'the_geom',
        }

        return params


class CensusPostcodeDistricts(CensusPostcodeEntitiesFromOAs):
    def requires(self):
        deps = {
            'source_geom_columns': OutputAreaColumns(),
            'source_geom': OutputAreas(),
            'source_data_columns': CensusColumns(),
            'source_data': CensusOutputAreas(),
            'target_geom_columns': PostcodeDistrictsColumns(),
            'target_geom': PostcodeDistricts(),
            'target_data_columns': CensusColumns(),
        }

        return deps


class CensusPostcodeSectors(CensusPostcodeEntitiesFromOAs):
    def requires(self):
        deps = {
            'source_geom_columns': OutputAreaColumns(),
            'source_geom': OutputAreas(),
            'source_data_columns': CensusColumns(),
            'source_data': CensusOutputAreas(),
            'target_geom_columns': PostcodeSectorsColumns(),
            'target_geom': PostcodeSectors(),
            'target_data_columns': CensusColumns(),
        }

        return deps


class CensusSOAsFromOAs(CoupledInterpolationTask):
    '''
    As the SOAs and OAs layers are coupled and SOAs are bigger than OAs,
    calculating the measurements for the SOAs is a matter of adding up
    the values, so the data for the Super Output Areas is currently extracted
    from the Output Areas.
    '''

    def table_timespan(self):
        return get_timespan('2011')

    def columns(self):
        cols = OrderedDict()
        input_ = self.input()
        cols['GeographyCode'] = input_['target_geom_columns']['geographycode']
        cols.update(input_['target_data_columns'])
        return cols

    def get_interpolation_parameters(self):
        params = {
            'source_data_geoid': 'geographycode',
            'source_geom_geoid': 'oa_sa',
            'target_data_geoid': 'geographycode',
            'target_geom_geoid': 'geographycode',
            'source_geom_geomfield': 'the_geom',
            'target_geom_geomfield': 'the_geom',
        }

        return params


class CensusLowerSuperOutputAreas(CensusSOAsFromOAs):
    def requires(self):
        deps = {
            'source_geom_columns': OutputAreaColumns(),
            'source_geom': OutputAreas(),
            'source_data_columns': CensusColumns(),
            'source_data': CensusOutputAreas(),
            'target_geom_columns': LowerLayerSuperOutputAreasColumns(),
            'target_geom': LowerLayerSuperOutputAreas(),
            'target_data_columns': CensusColumns(),
        }

        return deps


class CensusMiddleSuperOutputAreas(CensusSOAsFromOAs):
    def requires(self):
        deps = {
            'source_geom_columns': OutputAreaColumns(),
            'source_geom': OutputAreas(),
            'source_data_columns': CensusColumns(),
            'source_data': CensusOutputAreas(),
            'target_geom_columns': MiddleLayerSuperOutputAreasColumns(),
            'target_geom': MiddleLayerSuperOutputAreas(),
            'target_data_columns': CensusColumns(),
        }

        return deps
