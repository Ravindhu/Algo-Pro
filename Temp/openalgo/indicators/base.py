# -*- coding: utf-8 -*-
"""
OpenAlgo Technical Indicators - Base Class
"""

import numpy as np
import pandas as pd
from openalgo.numba_shim import jit
from abc import ABC, abstractmethod
from typing import Union, Tuple, Optional, List

class BaseIndicator(ABC):
    """Base class for all technical indicators"""
    
    def __init__(self, name: str):
        self.name = name
    
    @abstractmethod
    def calculate(self, *args, **kwargs):
        """Calculate the indicator values"""
        pass
    
    @staticmethod
    def validate_input(data: Union[np.ndarray, pd.Series, list]) -> Tuple[np.ndarray, str, Optional[pd.Index]]:
        """
        Validate and convert input data to numpy array while preserving type information
        
        Parameters:
        -----------
        data : Union[np.ndarray, pd.Series, list]
            Input data to validate
            
        Returns:
        --------
        Tuple[np.ndarray, str, Optional[pd.Index]]
            (validated numpy array, input_type, index)
            input_type: 'pandas', 'numpy', or 'list'
            index: pandas index if input was pandas Series, None otherwise
            
        Raises:
        -------
        TypeError
            If input type is not supported
        ValueError
            If input data is empty
        """
        if isinstance(data, pd.Series):
            if len(data) == 0:
                raise ValueError("Input data cannot be empty")
            return data.values.astype(np.float64), 'pandas', data.index
        elif isinstance(data, list):
            if len(data) == 0:
                raise ValueError("Input data cannot be empty")
            return np.array(data, dtype=np.float64), 'list', None
        elif isinstance(data, np.ndarray):
            if data.size == 0:
                raise ValueError("Input data cannot be empty")
            return data.astype(np.float64), 'numpy', None
        else:
            raise TypeError(f"Invalid input type: {type(data)}. Expected np.ndarray, pd.Series, or list")
    
    @staticmethod
    def format_output(result: np.ndarray, input_type: str, index: Optional[pd.Index] = None) -> Union[np.ndarray, pd.Series]:
        """
        Format output to match input type
        
        Parameters:
        -----------
        result : np.ndarray
            Result array to format
        input_type : str
            Type of original input ('pandas', 'numpy', or 'list')
        index : Optional[pd.Index]
            Original pandas index if input was pandas Series
            
        Returns:
        --------
        Union[np.ndarray, pd.Series]
            Formatted result matching input type
        """
        if input_type == 'pandas':
            return pd.Series(result, index=index)
        else:
            return result
    
    @staticmethod
    def format_multiple_outputs(results: Tuple[np.ndarray, ...], input_type: str, 
                               index: Optional[pd.Index] = None) -> Union[Tuple[np.ndarray, ...], Tuple[pd.Series, ...]]:
        """
        Format multiple outputs to match input type
        
        Parameters:
        -----------
        results : Tuple[np.ndarray, ...]
            Result arrays to format
        input_type : str
            Type of original input ('pandas', 'numpy', or 'list')
        index : Optional[pd.Index]
            Original pandas index if input was pandas Series
            
        Returns:
        --------
        Union[Tuple[np.ndarray, ...], Tuple[pd.Series, ...]]
            Formatted results matching input type
        """
        if input_type == 'pandas':
            return tuple(pd.Series(result, index=index) for result in results)
        else:
            return results
    
    @staticmethod
    def validate_period(period: int, data_length: int) -> None:
        """
        Validate period parameter
        
        Parameters:
        -----------
        period : int
            Period value to validate
        data_length : int
            Length of the data array
            
        Raises:
        -------
        ValueError
            If period is invalid
        """
        if not isinstance(period, int):
            raise TypeError(f"Period must be an integer, got {type(period)}")
        if period <= 0:
            raise ValueError(f"Period must be positive, got {period}")
        if period > data_length:
            raise ValueError(f"Period ({period}) cannot be greater than data length ({data_length})")
    
    @staticmethod
    def handle_nan(arr: np.ndarray, fill_value: float = 0.0) -> np.ndarray:
        """
        Handle NaN values in the array
        
        Parameters:
        -----------
        arr : np.ndarray
            Array with potential NaN values
        fill_value : float
            Value to replace NaN with
            
        Returns:
        --------
        np.ndarray
            Array with NaN values handled
        """
        return np.nan_to_num(arr, nan=fill_value)
    
    @staticmethod
    def align_arrays(*arrays: np.ndarray) -> Tuple[np.ndarray, ...]:
        """
        Align multiple arrays to the same length
        
        Parameters:
        -----------
        *arrays : np.ndarray
            Variable number of arrays to align
            
        Returns:
        --------
        Tuple[np.ndarray, ...]
            Tuple of aligned arrays
            
        Raises:
        -------
        ValueError
            If arrays have different lengths
        """
        if not arrays:
            return tuple()
        
        lengths = [len(arr) for arr in arrays]
        if len(set(lengths)) > 1:
            raise ValueError(f"All arrays must have the same length. Got lengths: {lengths}")
        
        return arrays
    
    @staticmethod
    def rolling_window(arr: np.ndarray, window: int) -> np.ndarray:
        """
        Create rolling window view of array (Pure NumPy - no Numba due to as_strided)
        
        Parameters:
        -----------
        arr : np.ndarray
            Input array
        window : int
            Window size
            
        Returns:
        --------
        np.ndarray
            2D array with rolling windows
        """
        shape = arr.shape[:-1] + (arr.shape[-1] - window + 1, window)
        strides = arr.strides + (arr.strides[-1],)
        return np.lib.stride_tricks.as_strided(arr, shape=shape, strides=strides)
    
    @staticmethod
    @jit(nopython=True)
    def rolling_window_numba(arr: np.ndarray, window: int) -> np.ndarray:
        """
        Create rolling window data using Numba-compatible approach
        
        Parameters:
        -----------
        arr : np.ndarray
            Input array
        window : int
            Window size
            
        Returns:
        --------
        np.ndarray
            2D array with rolling windows
        """
        n = len(arr)
        if n < window:
            return np.empty((0, window))
        
        result = np.empty((n - window + 1, window))
        for i in range(n - window + 1):
            for j in range(window):
                result[i, j] = arr[i + j]
        
        return result