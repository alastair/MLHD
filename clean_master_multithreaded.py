# A master script to clean and export MLHD data!

####
### Importing Libraries ###
####

# Essential Imports
from lib import io_ as io
from time import monotonic
from rich.progress import track
from os.path import join
from numpy import nan
import pandas as pd
import concurrent.futures

import config
import clean_master_config as cmc

# For pretty CLI
from rich import print
from rich.console import Console
console = Console()
console.clear()

####
### Getting started ###
####

io.generate_folders()               # Generate folders to write all outputs (as specified in config.py)
master_start = monotonic()          # Start the master timer

OUTPUT_LOG = {}                     # Log Progress
TIME_LOGS = {}                      # Logs time taken for each step

# Loading ENV variables
console.log("Loading ENV variables...")

MLHD_ROOT = config.MLHD_ROOT
WRITE_ROOT = config.WRITE_ROOT
LOG_WRITE_ROOT = config.LOG_WRITE_ROOT
LOG_WRITE_PATH = join(LOG_WRITE_ROOT, cmc.LOG_FILE_NAME)
LOG_EPOCH = config.LOG_EPOCH

# Fetching a 1 Dimensional list of MLHD file paths
console.log("Generating MLHD Paths...")
MLHD_PATHS = io.generate_paths(MLHD_ROOT)

####
### LOADING MB TABLES ###
####

TIME_LOGS['MB_start'] = monotonic()

console.log('loading recording gids...')
MB_rec_gid = pd.read_parquet('warehouse/MB_tables/recording_gid.parquet')
MB_rec_gid.set_index('gid', inplace=True)

console.log('loading recording redirects...')
MB_rec_redirects = pd.read_parquet('warehouse/MB_tables/recording_redirects.parquet')
MB_rec_redirects.set_index('old', inplace=True)

console.log('loading recording canonical MBIDs...')
MB_rec_canonical = pd.read_parquet('warehouse/MB_tables/recording_canonical.parquet')
MB_rec_canonical.set_index('old', inplace=True)

console.log('loading artist credit gids...')
MB_artist_credit_list = pd.read_parquet('warehouse/MB_tables/artist_credit_release_gid.parquet')
MB_artist_credit_list.set_index('recording_mbid', inplace=True)
MB_artist_credit_list['artist_mbids'] = MB_artist_credit_list.artist_mbids.map(lambda x: x.strip('{}'))

# Converting MB_rec_gid to set for faster lookup
rec_gid_set = set(MB_rec_gid.index)

TIME_LOGS['MB_end'] = monotonic()
console.log("loaded MB tables. Took {} seconds".format(round(TIME_LOGS['MB_end'] - TIME_LOGS['MB_start'], 2)))

####
### Defining Functions ###
####

# Main function to process dataframe
def process_df(df_input, process_timings_list, keep_missing = cmc.KEEP_MISSING, turn_blank = cmc.TURN_BLANK):
    """Take an input df and process it into a cleaned df

    Args:
        df_input (pandas.DataFrame): input dataframe with columns: <timestamp, artist_MBID, release_MBID, recording_MBID>
        keep_missing (bool, optional): If True, keep rows with missing, unknown MBIDs to maintain the structure of the original data.
        turn_blank (bool, optional): If True, replace blank MBIDs with None

    Returns:
        pandas.DataFrame: Cleaned dataframe with columns: <timestamp, artist_MBID, release_MBID, recording_MBID>
    """
    start = monotonic()
    # 1. Get redirects for MBIDs that aren't present in rec_gid_set using MB_rec_redirects.
    df_input['recording_MBID'] = df_input['recording_MBID'].map(
        lambda x: io.replace(x, MB_rec_redirects, 'new') 
        if x not in rec_gid_set else x)

    # 2. Find canonical recordings for all cleaned/uncleaned recording_MBIDs
    df_input['recording_MBID'] = df_input['recording_MBID'].map(
        lambda x: io.replace(x, MB_rec_canonical, 'new')
        if io.replace(x, MB_rec_canonical, 'new') is not nan else x)

    # 3. Fetch artist, release_MBIDs for all recording_MBIDs
    artist_release_mbids = df_input['recording_MBID'].map(
        lambda x: io.replace_multi(x, MB_artist_credit_list))
    
    df_input[['artist_MBID', 'release_MBID']] = pd.DataFrame(
        artist_release_mbids.tolist(), 
        columns = ['artist_MBID', 'release_MBID'], 
        index=df_input.index)
    end = monotonic()
    process_timings_list.append(round(end - start, 2))
    
    return df_input

chunker = lambda input_list, batch_size: [input_list[i:i+batch_size] for i in range(0, len(input_list), batch_size)]

def load_chunk(path_list_chunk):
    output = []
    load_timings = []
    
    for path in path_list_chunk:
        start_load = monotonic()
        
        output.append(io.load_path(path))
        
        end_load = monotonic()
        load_timings.append(round(end_load - start_load, 2))

    return output, load_timings

# def process_chunk(df_list_chunk):
#     output = []
#     process_timings = []
#     for df in df_list_chunk:
#         start_process = monotonic()

#         output.append(process_df(df))
        
#         end_process = monotonic()
#         process_timings.append(round(end_process - start_process, 2))

#     return output, process_timings

def write_chunk(chunk, path_lists):
    output = []
    write_timings = []
    
    for df, path in zip(chunk, path_lists):
        start_write = monotonic()

        output.append(io.write_frame(df, path))
        
        end_write = monotonic()
        write_timings.append(round(end_write - start_write, 2))

    return output, write_timings

# Driver function to read, clean, and write all the file_paths in the path_list, while logging their details
def driver(
    path_list, 
    keep_missing = cmc.KEEP_MISSING, 
    turn_blank = cmc.TURN_BLANK, 
    write_root = config.WRITE_ROOT):

    """Driver function to read, clean, and write all the file_paths in the path_list, while logging their details

    Args:
        path_list (list): List of paths to the tables to be cleaned
        keep_missing (bool, optional): If True, keep rows with missing, unknown MBIDs to maintain the structure of the original data. Defaults to cmc.KEEP_MISSING.
        turn_blank (bool, optional): If True, replace blank MBIDs with None. Defaults to cmc.TURN_BLANK
        write_root (str, optional): Root directory to write the cleaned tables to. Defaults to config.WRITE_ROOT.
    Returns:
        list: List of cleaned dataframes
    """
    console.log("Looping through MLHD files...")  
    file_counter = 0
    start_loop = monotonic()

    chunks = chunker(path_list, cmc.CHUNK_SIZE)
    for i, chunk in enumerate(chunks):
        # 1. Load chunk
        console.log(f'Loading Chunk {i}...')
        df_list, load_timings = load_chunk(chunk)
        
        # with concurrent.futures.ThreadPoolExecutor() as executor:
        #     df_list, load_timings = executor.map(load_chunk, chunk)

        console.log(f'Loaded Chunk {i}. Took {format(round(sum(load_timings), 2))} seconds')

        # 2. Process chunk
        console.log(f'Processing Chunk {i}...')
        # df_list, process_timings = process_chunk(df_list)
        process_timings = []
        df_list = map(lambda x: process_df(x, process_timings), df_list)
        
        # with concurrent.futures.ThreadPoolExecutor(max_workers=cmc.MAX_WORKERS) as executor:
        #     df_list = executor.map(lambda x: process_df(x, process_timings), df_list)
        
        # with concurrent.futures.ProcessPoolExecutor(max_workers=cmc.MAX_WORKERS) as executor:
        #     df_list = executor.map(lambda x: process_df(x, process_timings), df_list)
        

        console.log(f'Processed Chunk {i}. Took {format(round(sum(process_timings), 2))} seconds')

        # 3. Write chunk
        console.log(f'Writing Chunk {i}...')
        write_paths, write_timings = write_chunk(df_list, chunk)
        
        console.log(f'Wrote Chunk {i}. Took {format(round(sum(write_timings), 2))} seconds')

        # 4. Logging outputs
        file_counter += len(chunk)
        file_counter += 1
        io.log_output(
            "",
            write_paths,
            [
                load_timings,
                process_timings, 
                write_timings
            ], 
            monotonic(),
            OUTPUT_LOG
            )
        
        if file_counter%LOG_EPOCH == 0:
            _ = io.write_log(OUTPUT_LOG, LOG_WRITE_PATH)

        # console.log("Finished processing {} files. Took {} seconds".format(
        #     file_counter, round(monotonic() - start_loop, 2)))
    
    end_loop = monotonic()
    loop_time = round(end_loop - start_loop, 2)

    console.log(f"Looped through {len(path_list)} files in {loop_time} seconds")
    return None

####
### Running the driver function ###
####

start_process = monotonic()

# with concurrent.futures.ThreadPoolExecutor(max_workers=cmc.MAX_WORKERS) as executor:
#     executor.map(driver, MLHD_PATHS[:10])

# with concurrent.futures.ProcessPoolExecutor(max_workers=cmc.MAX_WORKERS) as executor:
#     executor.map(driver, MLHD_PATHS[:10])

driver(MLHD_PATHS[:15])

end_process = monotonic()
####
### Outro ###
####

master_end = monotonic()                                #End the master timer
process_time = round(end_process - start_process, 2)    #Calculate the time taken to process the data
master_time = round(master_end - master_start, 2)       #Calculate the master time

master_log = {
    "Master time": master_time,
    "Process time": process_time,
    "Log_Path": LOG_WRITE_PATH
    }

io.write_log(master_log, LOG_WRITE_PATH.replace('.json', '_master.json'))            #Write the master log

console.log(f"Finished Process in {master_time} seconds")
console.log(f"Output log written to {LOG_WRITE_PATH}")