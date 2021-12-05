"""
Provides the following multilateral methods:

* :func:`time_dummy`
* :func:`geary_khamis`
* :func:`geks`
paired with
    * :func:`carli`
    * :func:`jevons`
    * :func:`dutot`
    * :func:`laspeyres`
    * :func:`paasche`
    * :func:`geom_laspeyres`
    * :func:`geom_paasche`
    * :func:`drobish`
    * :func:`marshall_edgeworth`
    * :func:`palgrave`
    * :func:`fisher`
    * :func:`tornqvist`
    * :func:`walsh`
    * :func:`sato_vartia`
    * :func:`geary_khamis_b`
    * :func:`tpd`
    * :func:`rothwell`

* :func:`time_dummy_pyspark`
* :func:`geary_khamis_pyspark`

The TDH/TPD methods are model-based multilateral index number methods
which have been proposed to incorporate scanner data. They are part of
many multilateral methods motivated by an attempt to minimize the risk
of chain drift, particularly within a window, while maximizing the
number of matches in the data.

TDH index is used when information on item characteristics are
available, and the TPD index when this information is lacking. The
TDH produces an explicit hedonic price index, while the TPD produces
an implicit hedonic price index, which are both estimated on the
pooled data of one or more periods via an application of expenditure
shares weighted least squares regression.
"""

from typing import List, Sequence, Optional
from itertools import combinations
import time

import pandas as pd
import numpy as np
from scipy.stats.mstats import gmean
from pyspark.mllib.linalg.distributed import (
    IndexedRow,
    IndexedRowMatrix,
    CoordinateMatrix,
    MatrixEntry,
    DenseMatrix,
)
from pyspark.ml.feature import VectorAssembler
from pyspark.sql import (
    DataFrame as SparkDF,
    functions as F,
)
from pyspark import SparkContext

from bilateral_methods import *
from helpers import diag, weights_calc
from wls import wls

__author__ = ['Dr. Usman Kayani']

def geks(
    df: pd.DataFrame,
    price_col: str = 'price',
    quantity_col: str = 'quantity',
    date_col: str='month',
    product_id_col: str='id',
    bilateral_method: str = 'tornqvist',
) -> List:
    """
    Obtain the GEKS indices paired with a bilateral method for a given dataframe.

    Calculate the index values using a for loop to determine the matrix of
    bilaterals, where we exploit the symmetry condition a_{i j} = 1/a_{j i} and
    a_{i i} = 1 to save computation time, followed by a geometric mean.
    """
    # Get unique periods and length of time series.
    periods = df[date_col].unique()
    no_of_periods = len(periods)

    if bilateral_method != 'tpd':
        # Obtain bilateral function for bilateral method.
        bilateral_func = globals()[bilateral_method]

    # Intialize matrix for bilateral pairs.
    pindices = np.zeros((no_of_periods, no_of_periods))

    for month_idx in combinations(range(no_of_periods), 2):
        # Get period index for base and current month, and slice df for these
        # months.
        i, j = month_idx
        df_base = df.loc[df[date_col] == periods[i]]
        df_curr = df.loc[df[date_col] == periods[j]]

        # Make sure the sample is matched for given periods.
        df_base = df_base[df_base[product_id_col].isin(df_curr[product_id_col])]
        df_curr = df_curr[df_curr[product_id_col].isin(df_base[product_id_col])]

        if bilateral_method == 'tpd':
            # Use multilateral TPD method with two periods.
            df_matched = (
                pd.concat([df_base, df_curr])
                .drop_duplicates()
                .drop(columns='weights') 
            )
            # Recalculate weights for matched df.
            df_matched = weights_calc(df_matched)
            # Append values to upper triangular of matrix.
            pindices[i, j] = time_dummy(df_matched)[-1]
        else:
            # Find price and quantity vectors of base period and current period.
            p_base = df_base[price_col].to_numpy()
            p_curr = df_curr[price_col].to_numpy()
            data = (p_base, p_curr)

            # Get quantities for bilateral methods that use this information.
            if bilateral_method in {
                'laspeyres', 'drobish', 'marshall_edgeworth',
                'geom_laspeyres', 'tornqvist', 'fisher',
                'walsh', 'sato_vartia', 'geary_khamis_b', 
                'rothwell'
            }:
                q_base = df_base[quantity_col].to_numpy()
                data += (q_base, )
            if bilateral_method in {
                'paasche', 'drobish','palgrave',
                'marshall_edgeworth', 'geom_paasche', 'tornqvist',
                'fisher', 'walsh', 'sato_vartia',
                'geary_khamis_b'
            }:
                q_curr = df_curr[quantity_col].to_numpy()
                data += (q_curr, )

            # Determine the bilaterals for each base and current period and
            # append to upper tringular of matrix.
            pindices[i, j] = bilateral_func(*data)

    # Exploit symmetry conditions for matrix of bilaterals.
    pindices_sym = np.copy(pindices.T)
    mask = pindices_sym != 0
    pindices_sym[mask] = 1/pindices_sym[mask]
    pindices += pindices_sym + np.identity(no_of_periods)

    # Calculate geometric mean for the unnormalized price levels.
    pgeo = gmean(pindices)

    # Normalize to first period.
    return pgeo/pgeo[0]

def time_dummy(
    df: pd.DataFrame,
    price_col: str = 'price',
    quantity_col: str = 'quantity',
    date_col: str = 'month',
    product_id_col: str = 'id',
    characteristics: Optional[Sequence[str]] = None,
    engine: str = 'numpy'
) -> List:
    """Obtain the time dummy indices for a given dataframe.

    Calculates the time dummy indices using a formula with weighted least
    squares regression.  When passed with characteristics, this function returns
    the Time Dummy Hedonic indices. When passed without it returns the Time
    Product Dummy indices.
    """
    # Set the dtype for ID columns, in case it is numerical.
    df[product_id_col] = df[product_id_col].astype(str)

    # Calculate logarithm of the prices for each item for dependent variable.
    df['log_price'] = np.log(df[price_col])

    # Get terms for wls regression where characteristics are used if available.
    non_time_vars = characteristics if characteristics else [product_id_col]

    model_params = wls(
        df,
        dependent_var='log_price',
        independent_vars=[date_col, *non_time_vars],
        engine=engine
    )

    # Get indices from the time dummy coefficients & set first = 1.
    is_time_dummy = model_params.index.str.contains(date_col)

    return [1, *np.exp(model_params.loc[is_time_dummy])]

def geary_khamis(
    df: pd.DataFrame,
    price_col: str = 'price',
    quantity_col: str = 'quantity',
    date_col: str = 'month',
    product_col: str = 'id',
) -> List:
    """Obtain the Geary-Khamis indices for a given dataframe.

    Calculates the Geary-Khamis indices using matrix operations.
    """
    # We pivot the dataframe for the required vectors and matrices, and fillna
    # to deal with missing items.
    df = (
        df.pivot_table(index=product_col, columns=date_col)
        .fillna(0)
    )

    # Get number of unique products for the size of the vectors and matrices.
    N = len(df.index)

    # Matrices for the prices, quantities and weights.
    prices = df[price_col]
    quantities = df[quantity_col]
    weights = df['weights']

    # Inverse of diagonal matrix with total quantities for each good over all
    # time periods as diagonal elements and matrix product of weights and
    # transpose of quantities to produce a square matrix, both required for C
    # matrix.
    q_matrix_inverse = np.diag(1/quantities.T.sum())
    prod_weights_qt_matrix = weights @ quantities.T

    # Product of above matrices to give the C square matrix, with the fixed
    # identity and R matrix, and c vector all required determine the quality
    # adjustment factors b.
    C_matrix = q_matrix_inverse @ prod_weights_qt_matrix
    R_matrix = np.zeros(shape=(N, N))
    R_matrix[:1] = 1

    # Calculation of the vector b required to produce the price levels.
    # Corresponds to `b = [I_n - C + R]^-1 [1,0,..,0]^T`
    b = np.linalg.pinv(np.identity(N) - C_matrix + R_matrix) @ np.eye(N, 1)

    # Determine price levels to compute the final index values.
    price_levels = diag(prices.T @ quantities).div(quantities.T @ b)

    # Normalize price levels to first period for final index values.
    index_vals = price_levels / price_levels.iloc[0]

    return index_vals.iloc[:, 0].tolist()

def time_dummy_pyspark(
    df: SparkDF,
    Number_of_periods: int,
    price_col: str = 'price',
    date_col: str = 'month',
    product_id_col: str = 'id',
    characteristics: Optional[Sequence[str]] = None,
) -> List:
    """Obtain the time dummy indices for a given dataframe in PySpark.

    Calculates the time dummy indices using a formula with weighted least
    squares regression.  When passed with characteristics, this function returns
    the Time Dummy Hedonic indices. When passed without it returns the Time
    Product Dummy indices.
    """  
    # Calculate logarithm of the prices for each item.
    df = df.withColumn('log_price', F.log(price_col))

    non_time_vars = characteristics if characteristics else [product_id_col]

    # WLS regression with labels, features & weights -> fit model.
    model = (
        wls(
            df,
            dependent_var='log_price',
            independent_vars=[date_col, *non_time_vars],
            engine='pyspark'
        )
    )

    # Extracting time dummy coefficients.
    time_dummy_coeff = model.coefficients[:Number_of_periods-1][::-1]
    
    # Get indices from the time dummy coefficients & set first = 1.
    return [1, *np.exp(time_dummy_coeff)]

def geary_khamis_pyspark(
    df: SparkDF,
    sc: SparkContext,
    price_col: str = 'price',
    quantity_col: str = 'quantity',
    date_col: str = 'month',
    product_level: str = 'id',
) -> List:
    """Obtain the Geary-Khamis indices for a given dataframe in PySpark.

    Calculates the Geary-Khamis indices using matrix operations.
    """
    pivoted_df = (
        df.groupby(product_level)
        .pivot(date_col)
    )

    matrix_dfs = []

    for col in [price_col, quantity_col, 'weights']:
        matrix_dfs.append(
            pivoted_df
            .avg(col)
            .sort(product_level)
            .drop(product_level)
            .fillna(0)
        )
    cols = matrix_dfs[0].columns
    
    vectors = []
    for mdf in matrix_dfs:
        vectors.append(
            VectorAssembler(inputCols=cols, outputCol='vec')
            .transform(mdf)
            .select('vec')
            .collect()
        )
    
    N = len(vectors[0])
    M = len(cols)

    matrices = []
    for vec in vectors:
        matrices.append(
            IndexedRowMatrix(
                sc
                .range(N)
                .map(lambda i: IndexedRow(i, vec[i][0].array))
            )
        )
    
    qsum_arr = matrices[1].rows.map(lambda row: row.vector.sum()).collect()
    qsum_inv_mat = CoordinateMatrix(
        sc
        .range(N)
        .map(lambda i: MatrixEntry(i, i, 1/qsum_arr[i])), N, N
    )

    C = (
        qsum_inv_mat.toBlockMatrix()
        .multiply(matrices[2].toBlockMatrix())
        .multiply(matrices[1].toBlockMatrix().transpose())
    )


    Identity = (
        CoordinateMatrix(
            sc
            .range(N)
            .map(lambda i: MatrixEntry(i, i, 1.0)), N, N
        )
        .toBlockMatrix()
    )

    R = (
        CoordinateMatrix(
            sc
            .range(N)
            .map(lambda i: MatrixEntry(0, i, 1.0)), N, N
        )
        .toBlockMatrix()
    )

    svd = (
        R.add(Identity).subtract(C)
        .toIndexedRowMatrix()
        .computeSVD(N, True)
    )

    s = svd.s
    V = svd.V
    U = svd.U

    V_ = DenseMatrix(N, N, V.toArray().transpose().reshape(-1))
    invS = DenseMatrix(len(s), len(s), np.diag(1/s).reshape(-1))

    c_ = (
        CoordinateMatrix(
            sc
            .range(N)
            .map(lambda i: MatrixEntry(0, 0, 1/N)), N, 1
        )
        .toBlockMatrix()
        .transpose()
    )

    b = (
        c_.multiply(U.multiply(invS).multiply(V_).toBlockMatrix())
        .toLocalMatrix()
        .toArray()
    )

    mat_prices_ = (
        matrices[0]
        .toBlockMatrix()
        .transpose()
        .toLocalMatrix()
        .toArray()
    )

    mat_quantities_ = (
        matrices[1]
        .toBlockMatrix()
        .transpose()
        .toLocalMatrix()
        .toArray()
    )

    price_levels = []
    for i in range(M):
        val = mat_prices_[i].dot(mat_quantities_[i])/mat_quantities_[i].dot(b[0])
        price_levels.append(val)

    initial_pl = price_levels[0]

    return [pl/initial_pl for pl in price_levels]