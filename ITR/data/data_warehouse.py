import warnings  # needed until apply behaves better with Pint quantities in arrays
import pandas as pd
import numpy as np

from abc import ABC
from typing import List, Type
from pydantic import ValidationError

import ITR
from ITR.data.osc_units import ureg, Q_, asPintSeries
from ITR.interfaces import EScope, IEmissionRealization, IEIRealization, ICompanyData, ICompanyAggregates, ICompanyEIProjection, ICompanyEIProjections, DF_ICompanyEIProjections
from ITR.data.data_providers import CompanyDataProvider, ProductionBenchmarkDataProvider, IntensityBenchmarkDataProvider
from ITR.configs import ColumnsConfig, TemperatureScoreConfig, LoggingConfig

import logging
logger = logging.getLogger(__name__)
LoggingConfig.add_config_to_logger(logger)

import pint

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class DataWarehouse(ABC):
    """
    General data provider super class.
    """

    def __init__(self, company_data: CompanyDataProvider,
                 benchmark_projected_production: ProductionBenchmarkDataProvider,
                 benchmarks_projected_ei: IntensityBenchmarkDataProvider,
                 estimate_missing_data=None,
                 column_config: Type[ColumnsConfig] = ColumnsConfig):
        """
        Create a new data warehouse instance.

        :param company_data: CompanyDataProvider
        :param benchmark_projected_production: ProductionBenchmarkDataProvider
        :param benchmarks_projected_ei: IntensityBenchmarkDataProvider
        """
        self.benchmark_projected_production = benchmark_projected_production
        self.benchmarks_projected_ei = benchmarks_projected_ei
        # benchmarks_projected_ei._EI_df is the EI dataframe for the benchmark
        # benchmark_projected_production.get_company_projected_production(company_sector_region_scope) gives production data per company (per year)
        # multiplying these two gives aligned emissions data for the company, in case we want to add missing data based on sector averages
        self.column_config = column_config
        self.company_data = company_data
        # If we are missing S3 (or other) data, fill in before projecting targets
        # Because we have already filled in historic_data and projected_intensities, we have to recompute some of those
        if estimate_missing_data is not None:
            for c in self.company_data._companies:
                estimate_missing_data(self, c)
        self.company_data._calculate_target_projections(benchmark_projected_production)
        self.company_scope = {}

        assert getattr(benchmarks_projected_ei._EI_benchmarks, 'S1S2') or (getattr(benchmarks_projected_ei._EI_benchmarks, 'S1') == None)
        if (getattr(benchmarks_projected_ei._EI_benchmarks, 'S1S2')
            and benchmarks_projected_ei._EI_benchmarks['S1S2'].production_centric):
            # Production-Centric benchmark: After projections have been made, shift S3 data into S1S2.
            # If we shift before we project, then S3 targets will not be projected correctly.
            logger.info(
                f"Shifting S3 emissions data into S1 according to Production-Centric benchmark rules"
            )
            for c in self.company_data._companies:
                if c.ghg_s3:
                    # For Production-centric and energy-only data (except for Cement), convert all S3 numbers to S1 numbers
                    if not ITR.isnan(c.ghg_s3.m):
                        c.ghg_s1s2 = c.ghg_s1s2 + c.ghg_s3
                    c.ghg_s3 = None # Q_(0.0, c.ghg_s3.u)
                if c.historic_data:
                    def _adjust_historic_data(data, primary_scope_attr, data_adder):
                        if getattr (data, primary_scope_attr):
                            pre_s3_data = [p for p in getattr (data, primary_scope_attr) if p.year <= data.S3[0].year]
                            if len(pre_s3_data)==0:
                                # Could not adjust
                                breakpoint()
                                return
                            if len(pre_s3_data)>1:
                                pre_s3_data = list( map(lambda x: type(x)(year=x.year, value=data.S3[0].value * x.value / pre_s3_data[-1].value), pre_s3_data[:-1]) )
                                s3_data = pre_s3_data + data.S3
                            else:
                                s3_data = data.S3
                            setattr (data, primary_scope_attr, list( map(data_adder, getattr (data, primary_scope_attr), s3_data)))
                        else:
                            setattr (data, primary_scope_attr, data.S3)
                            
                    if c.historic_data.emissions and c.historic_data.emissions.S3:
                        _adjust_historic_data(c.historic_data.emissions, 'S1', IEmissionRealization.add)
                        _adjust_historic_data(c.historic_data.emissions, 'S1S2', IEmissionRealization.add)
                        c.historic_data.emissions.S3 = []
                    if c.historic_data.emissions and c.historic_data.emissions.S1S2S3:
                        # assert c.historic_data.emissions.S1S2 == c.historic_data.emissions.S1S2S3
                        c.historic_data.emissions.S1S2S3 = []
                    if c.historic_data.emissions_intensities and c.historic_data.emissions_intensities.S3:
                        _adjust_historic_data(c.historic_data.emissions_intensities, 'S1', IEIRealization.add)
                        _adjust_historic_data(c.historic_data.emissions_intensities, 'S1S2', IEIRealization.add)
                        c.historic_data.emissions_intensities.S3 = []
                    if c.historic_data.emissions_intensities and c.historic_data.emissions_intensities.S1S2S3:
                        # assert c.historic_data.emissions_intensities.S1S2 == c.historic_data.emissions_intensities.S1S2S3
                        c.historic_data.emissions_intensities.S1S2S3 = []
                if c.projected_intensities and c.projected_intensities.S3:
                    def _adjust_trajectories(trajectories, primary_scope_attr):
                        if not getattr (trajectories, primary_scope_attr):
                            setattr (trajectories, primary_scope_attr, trajectories.S3)
                        else:
                            if isinstance(trajectories.S3.projections, pd.Series):
                                getattr (trajectories, primary_scope_attr).projections = (
                                    getattr (trajectories, primary_scope_attr).projections + trajectories.S3.projections)
                            else:
                                breakpoint()
                                getattr (trajectories, primary_scope_attr).projections = list( map(ICompanyEIProjection.add, getattr (trajectories, primary_scope_attr).projections, trajectories.S3.projections))
                            
                    _adjust_trajectories(c.projected_intensities, 'S1')
                    _adjust_trajectories(c.projected_intensities, 'S1S2')
                    c.projected_intensities.S3 = None
                    if c.projected_intensities.S1S2S3:
                        # assert c.projected_intensities.S1S2.projections == c.projected_intensities.S1S2S3.projections
                        c.projected_intensities.S1S2S3 = None
                if c.projected_targets and c.projected_targets.S3:
                    # For production-centric benchmarks, S3 emissions are counted against S1 (and/or the S1 in S1+S2)
                    def _align_and_sum_projected_targets(targets, primary_scope_attr):
                        primary_projections = getattr (targets, primary_scope_attr).projections
                        s3_projections = targets.S3.projections
                        if isinstance(s3_projections, pd.Series):
                            getattr (targets, primary_scope_attr).projections = (
                                getattr (targets, primary_scope_attr).projections + s3_projections)
                        else:
                            breakpoint()
                            if primary_projections[0].year < s3_projections[0].year:
                                while primary_projections[0].year < s3_projections[0].year:
                                    primary_projections = primary_projections[1:]
                            elif primary_projections[0].year > s3_projections[0].year:
                                while primary_projections[0].year > s3_projections[0].year:
                                    s3_projections = s3_projections[1:]
                            getattr (targets, primary_scope_attr).projections = list( map(ICompanyEIProjection.add, primary_projections, s3_projections) )

                    if c.projected_targets.S1:
                        _align_and_sum_projected_targets (c.projected_targets, 'S1')
                    try:
                        # S3 projected targets may have been synthesized from a netzero S1S2S3 target and might need to be date-aligned with S1S2
                        _align_and_sum_projected_targets (c.projected_targets, 'S1S2')
                    except AttributeError:
                        if c.projected_targets.S2:
                            logger.warning(f"Scope 1+2 target projections should have been created for {c.company_id}; repairing")
                            c.projected_targets.S1S2 = ICompanyEIProjections(ei_metric = c.projected_targets.S1.ei_metric,
                                                                             projections = list( map(ICompanyEIProjection.add, c.projected_targets.S1.projections, c.projected_targets.S2.projections) ))
                        else:
                            logger.warning(f"Scope 2 target projections missing from company with ID {c.company_id}; treating as zero")
                            c.projected_targets.S1S2 = ICompanyEIProjections(ei_metric = c.projected_targets.S1.ei_metric,
                                                                             projections = c.projected_targets.S1.projections)
                        if c.projected_targets.S3:
                            _align_and_sum_projected_targets (c.projected_targets, 'S1S2')
                        else:
                            logger.warning(f"Scope 3 target projections missing from company with ID {c.company_id}; treating as zero")
                    except ValueError:
                        logger.error(f"S1+S2 targets not aligned with S3 targets for company with ID {c.company_id}; ignoring S3 data")
                    c.projected_targets.S3 = None
                if c.projected_targets and c.projected_targets.S1S2S3:
                    # assert c.projected_targets.S1S2 == c.projected_targets.S1S2S3
                    c.projected_targets.S1S2S3 = None

        # Set scope information based on what company reports and what benchmark requres
        # benchmarks_projected_ei._EI_df makes life a bit easier...
        missing_company_scopes = []
        for c in self.company_data._companies:
            region = c.region
            try:
                bm_company_sector_region = benchmarks_projected_ei._EI_df.loc[c.sector, region]
            except KeyError:
                try:
                    region = 'Global'
                    bm_company_sector_region = benchmarks_projected_ei._EI_df.loc[c.sector, region]
                except KeyError:
                    missing_company_scopes.append(c.company_id)
                    continue
            scopes = benchmarks_projected_ei._EI_df.loc[c.sector, region].index.tolist()
            if len(scopes) == 1:
                self.company_scope[c.company_id] = scopes[0]
                continue
            for scope in [EScope.S1S2S3, EScope.S1S2, EScope.S1, EScope.S3]:
                if scope in scopes:
                    self.company_scope[c.company_id] = scope
                    break
            if c.company_id not in self.company_scope:
                missing_company_scopes.append(c.company_id)

        if missing_company_scopes:
            logger.warning(
                f"The following companies do not disclose scope data required by benchmark and will be removed: {missing_company_scopes}"
            )


    def estimate_missing_s3_data(self, company: ICompanyData) -> None:
        if not self.benchmarks_projected_ei._EI_benchmarks.S3 or company.historic_data.emissions.S3:
            return

        # best_prod.loc[s3_all_nan.index.set_levels(['production'], level='metric')]
        company_info_at_base_year = self.company_data.get_company_intensity_and_production_at_base_year([company.company_id])
        row0 = company_info_at_base_year.iloc[0]
        sector = row0.sector
        region = row0.region
        if (sector, region) in self.benchmarks_projected_ei._EI_df.index:
            pass
        elif (sector, "Global") in self.benchmarks_projected_ei._EI_df.index:
            region = "Global"
            company_info_at_base_year.loc[:, 'region'] = "Global"
        else:
            return
        if EScope.S3 not in self.benchmarks_projected_ei._EI_df.loc[(sector, region)].index:
            # Construction Buildings don't have an S3 scope defined
            return
        projected_production = self.benchmark_projected_production.get_company_projected_production(company_info_at_base_year)
        bm_ei_s3 = asPintSeries(self.benchmarks_projected_ei._EI_df.loc[(sector, region, EScope.S3)])
        s3_emissions = projected_production.iloc[0].mul(bm_ei_s3)
        try:
            s3_emissions = s3_emissions.astype('pint[Mt CO2]')
        except pint.errors.DimensionalityError:
            # Don't know how to deal with this funky intensity value
            logger.error(f"Production type {projected_production.iloc[0].dtype.units:~P} and intensity type {bm_ei_s3.dtype.units} don't multiply to t CO2e")
            return
        base_year = self.company_data.projection_controls.BASE_YEAR
        company.ghg_s3 = s3_emissions[base_year]
        assert company.ghg_s3 is not None
        company.historic_data.emissions.S3 = [IEmissionRealization(year=er.year, value=s3_emissions[er.year])
                                              for er in company.historic_data.emissions.S1S2
                                              if er.year>=base_year]
        company.historic_data.emissions.S1S2S3 = list( map(IEmissionRealization.add,
                                                           [em_s1s2 for em_s1s2 in company.historic_data.emissions.S1S2
                                                            if em_s1s2.year >= base_year],
                                                           [em_s3 for em_s3 in company.historic_data.emissions.S3
                                                            if em_s3.year >= company.historic_data.emissions.S1S2[0].year]) )
        company.historic_data.emissions_intensities.S3 = [IEIRealization(year=ei_r.year, value=bm_ei_s3[ei_r.year])
                                                          for ei_r in company.historic_data.emissions_intensities.S1S2
                                                          if ei_r.year>=base_year]
        company.historic_data.emissions_intensities.S1S2S3 = list( map(IEIRealization.add,
                                                                       [ei_s1s2 for ei_s1s2 in company.historic_data.emissions_intensities.S1S2
                                                                        if ei_s1s2.year >= base_year],
                                                                       [ei_s3 for ei_s3 in company.historic_data.emissions_intensities.S3
                                                                        if ei_s3.year >= company.historic_data.emissions_intensities.S1S2[0].year]) )
        if isinstance(company.projected_intensities.S1S2, DF_ICompanyEIProjections):
            company.projected_intensities.S3 = DF_ICompanyEIProjections(ei_metric=company.projected_intensities.S1S2.ei_metric,
                                                                        projections=bm_ei_s3[bm_ei_s3.index.intersection(company.projected_intensities.S1S2.projections.index)])
            company.projected_intensities.S1S2S3 = DF_ICompanyEIProjections(ei_metric=company.projected_intensities.S1S2.ei_metric,
                                                                            projections=company.projected_intensities.S1S2.projections+company.projected_intensities.S3.projections)
        elif isinstance(company.projected_intensities.S1, DF_ICompanyEIProjections):
            company.projected_intensities.S3 = DF_ICompanyEIProjections(ei_metric=company.projected_intensities.S1.ei_metric,
                                                                        projections=bm_ei_s3[bm_ei_s3.index.intersection(company.projected_intensities.S1.projections.index)])
        else:
            try:
                company.projected_intensities.S3 = ICompanyEIProjections(ei_metric=company.projected_intensities.S1S2.ei_metric,
                                                                         projections=[ICompanyEIProjection(year=eip.year, value=bm_ei_s3[eip.year])
                                                                                      for eip in company.projected_intensities.S1S2.projections])
                assert company.projected_intensities.S1S2.projections[0].year == company.projected_intensities.S3.projections[0].year
                company.projected_intensities.S1S2S3 = ICompanyEIProjections(ei_metric=company.projected_intensities.S1S2.ei_metric,
                                                                             projections=list( map(ICompanyEIProjection.add,
                                                                                                   company.projected_intensities.S1S2.projections,
                                                                                                   company.projected_intensities.S3.projections) ))
            except AttributeError:
                company.projected_intensities.S3 = ICompanyEIProjections(ei_metric=company.projected_intensities.S1.ei_metric,
                                                                         projections=[ICompanyEIProjection(year=eip.year, value=bm_ei_s3[eip.year])
                                                                                      for eip in company.projected_intensities.S1.projections])
            # Without valid S2 data, we don't have S1S2S3
        logger.info(f"Added S3 estimates for {company.company_id} (sector = {sector}, region = {region})")


    def get_preprocessed_company_data(self, company_ids: List[str]) -> List[ICompanyAggregates]:
        """
        Get all relevant data for a list of company ids. This method should return a list of ICompanyAggregates
        instances.

        :param company_ids: A list of company IDs (ISINs)
        :return: A list containing the company data and additional precalculated fields
        """

        company_data = self.company_data.get_company_data(company_ids)
        df_company_data = pd.DataFrame.from_records([c.dict() for c in company_data]).set_index(self.column_config.COMPANY_ID, drop=False)
        valid_company_ids = df_company_data.index.to_list()

        company_info_at_base_year = self.company_data.get_company_intensity_and_production_at_base_year(valid_company_ids)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # See https://github.com/hgrecco/pint-pandas/issues/128
            projected_production = self.benchmark_projected_production.get_company_projected_production(
                company_info_at_base_year) # .sort_index()

        # trajectories are projected from historic data and we are careful to fill all gaps between historic and projections
        # FIXME: we just computed ALL company data above into a dataframe.  Why not use that?
        projected_trajectories = self.company_data.get_company_projected_trajectories(valid_company_ids)
        df_trajectory = self._get_cumulative_emissions(
            projected_ei=projected_trajectories,
            projected_production=projected_production)

        projected_targets = self.company_data.get_company_projected_targets(valid_company_ids)
        # Ensure we haven't set any targets for scopes we are not prepared to deal with
        projected_targets = projected_targets.loc[projected_production.index.intersection(projected_targets.index)]
        # Fill in ragged left edge of projected_targets with historic data, interpolating where we need to
        for col, year_data in projected_targets.items():
            # year_data is an unruly collection of unit types, so need to check NaN values row by row
            mask = year_data.apply(lambda x: ITR.isnan(x.m))
            if mask.all():
                # No sense trying to do anything with left-side all-NaN columns
                projected_targets = projected_targets.drop(columns=col)
                continue
            if mask.any():
                projected_targets.loc[mask[mask].index, col] = projected_trajectories.loc[mask[mask].index, col]
            else:
                break

        df_target = self._get_cumulative_emissions(
            projected_ei=projected_targets,
            projected_production=projected_production)
        df_budget = self._get_cumulative_emissions(
            projected_ei=self.benchmarks_projected_ei.get_SDA_intensity_benchmarks(company_info_at_base_year),
            projected_production=projected_production)
        # df_trajectory_exceedance = self._get_exceedance_year(df_trajectory, df_budget, None)
        # df_target_exceedance = self._get_exceedance_year(df_target, df_budget, None)
        df_trajectory_exceedance = self._get_exceedance_year(df_trajectory, df_budget, self.company_data.projection_controls.TARGET_YEAR)
        df_target_exceedance = self._get_exceedance_year(df_target, df_budget, self.company_data.projection_controls.TARGET_YEAR)
        df_scope_data = pd.concat([df_trajectory.iloc[:, -1].rename(self.column_config.CUMULATIVE_TRAJECTORY),
                                   df_target.iloc[:, -1].rename(self.column_config.CUMULATIVE_TARGET),
                                   df_budget.iloc[:, -1].rename(self.column_config.CUMULATIVE_BUDGET),
                                   df_trajectory_exceedance.rename(f"{self.column_config.TRAJECTORY_EXCEEDANCE_YEAR}"),
                                   df_target_exceedance.rename(f"{self.column_config.TARGET_EXCEEDANCE_YEAR}")],
                                  axis=1)
        df_company_data = df_company_data.join(df_scope_data).reset_index('scope')
        na_company_mask = df_company_data.scope.isna()
        if na_company_mask.any():
            # Happens when the benchmark doesn't cover the company's supplied scopes at all
            logger.warning(
                f"Dropping companies with no scope data: {df_company_scope[na_company_mask].index.get_level_values(level='company_id').to_list()}"
            )
            df_company_data = df_company_data[~na_company_mask]
        df_company_data[self.column_config.BENCHMARK_GLOBAL_BUDGET] = \
            pd.Series([self.benchmarks_projected_ei.benchmark_global_budget] * len(df_company_data),
                      dtype='pint[Gt CO2]',
                      index=df_company_data.index)
        # ICompanyAggregates wants this Quantity as a `str`
        df_company_data[self.column_config.BENCHMARK_TEMP] = [str(self.benchmarks_projected_ei.benchmark_temperature)] * len(df_company_data)
        companies = df_company_data.to_dict(orient="records")
        aggregate_company_data = [ICompanyAggregates.parse_obj(company) for company in companies]
        return aggregate_company_data

    def _convert_df_to_model(self, df_company_data: pd.DataFrame) -> List[ICompanyAggregates]:
        """
        transforms Dataframe Company data and preprocessed values into list of ICompanyAggregates instances

        :param df_company_data: pandas Dataframe with targets
        :return: A list containing the targets
        """
        df_company_data = df_company_data.where(pd.notnull(df_company_data), None).replace(
            {np.nan: None})  # set NaN to None since NaN is float instance
        companies_data_dict = df_company_data.to_dict(orient="records")
        model_companies: List[ICompanyAggregates] = []
        for company_data in companies_data_dict:
            try:
                model_companies.append(ICompanyAggregates.parse_obj(company_data))
            except ValidationError:
                logger.warning(
                    "(one of) the input(s) of company %s is invalid and will be skipped" % company_data[
                        self.column_config.COMPANY_NAME])
                pass
        return model_companies

    def _get_cumulative_emissions(self, projected_ei: pd.DataFrame, projected_production: pd.DataFrame) -> pd.DataFrame:
        """
        get the weighted sum of the projected emission
        :param projected_ei: series of projected emissions intensities
        :param projected_production: PintArray of projected production amounts
        :return: cumulative emissions, by year, based on weighted sum of emissions intensity * production
        """
        # By picking only the rows of projected_production (columns of projected_production.T)
        # that match projected_ei (columns of projected_ei.T), the rows of the DataFrame are not re-sorted
        projected_emissions_t = projected_ei.T.mul(projected_production.T[projected_ei.T.columns])
        # If ever there were null values here, it would mess up cumsum.  The fix would be to
        # apply cumsum(axis=0) to asPintDataFrame(projected_emissions_t) and then return the transposed, normalized result
        assert projected_emissions_t.isna().any().any() == False
        cumulative_emissions = projected_emissions_t.T.cumsum(axis=1).astype('pint[Mt CO2]')
        return cumulative_emissions

    def _get_exceedance_year(self, df_subject: pd.DataFrame, df_budget: pd.DataFrame, budget_year: int=None) -> pd.Series:
        """
        :param df_subject: DataFrame of cumulative emissions values over time
        :param df_budget: DataFrame of cumulative emissions budget allowed over time
        :param budget_year: if not None, set the exceedence budget to that year; otherwise budget starts low and grows year-by-year
        :return: The furthest-out year where df_subject < df_budget, or np.nan if none
        Where the (df_subject-aligned) budget defines a value but df_subject doesn't have a value, return pd.NA
        Where the benchmark (df_budget) fails to provide a metric for the subject scope, return no rows
        """
        missing_subjects = df_budget.index.difference(df_subject.index)
        aligned_rows = df_budget.index.intersection(df_subject.index)
        # idxmax returns the first maximum of a series, but we want the last maximum of a series
        # Reversing the columns, the maximum remains the maximum, but the "first" is the furthest-out year

        df_subject = df_subject.loc[aligned_rows, ::-1].pint.dequantify()
        df_budget = df_budget.loc[aligned_rows, ::-1].pint.dequantify()
        # units are embedded in the column multi-index, so this check validates dequantify operation post-hoc
        assert (df_subject.columns == df_budget.columns).all()
        if budget_year:
            df_exceedance_budget = pd.DataFrame({(year, units): df_budget[(budget_year, units)]
                                                 for year, units in df_budget.columns if year < budget_year })
            df_budget.update(df_exceedance_budget)
        # pd.where operation requires DataFrames to be aligned
        df_aligned = df_subject.where(df_subject <= df_budget)
        # Drop the embedded units from the multi-index and find the first (meaning furthest-out) date of alignment
        df_aligned = df_aligned.droplevel(1, axis=1).apply(lambda x: x.first_valid_index(), axis=1)

        if len(missing_subjects):
            df_aligned = pd.concat([df_aligned,
                                    pd.Series(data=[pd.NA] * len(missing_subjects),
                                              index=missing_subjects)])
        df_exceedance = df_aligned.map(lambda x: self.company_data.projection_controls.BASE_YEAR if pd.isna(x)
                                       else pd.NA if x>=self.company_data.projection_controls.TARGET_YEAR
                                       else x).astype('Int64')
        return df_exceedance
