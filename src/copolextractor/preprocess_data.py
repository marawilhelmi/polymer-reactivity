import pandas as pd
from sklearn.decomposition import PCA
from openai import OpenAI
import copolextractor.utils as utils
import os
import json
import numpy as np


# Global cache for embeddings
embedding_cache = {}


def extract_nested_fields(df):
    """
    Extracts nested fields from the reaction_conditions column

    Args:
        df (pd.DataFrame): Input DataFrame containing reaction_conditions column

    Returns:
        pd.DataFrame: DataFrame with extracted fields as separate columns
    """
    # Extract fields from reaction_conditions
    if 'reaction_conditions' in df.columns:
        # Method
        df['method'] = df['reaction_conditions'].apply(
            lambda x: x.get('method') if isinstance(x, dict) else None
        )

        # Polymerization type
        df['polymerization_type'] = df['reaction_conditions'].apply(
            lambda x: x.get('polymerization_type') if isinstance(x, dict) else None
        )

        # Temperature
        df['temperature'] = df['reaction_conditions'].apply(
            lambda x: x.get('temperature') if isinstance(x, dict) else None
        )

        # Solvent
        df['solvent'] = df['reaction_conditions'].apply(
            lambda x: x.get('solvent') if isinstance(x, dict) else None
        )

        # Reaction constants
        df['r_values'] = df['reaction_conditions'].apply(
            lambda x: x.get('reaction_constants') if isinstance(x, dict) else None
        )

    return df


# Function: Filter data based on the filters
def filter_data(data):
    """
    Filters the input data to include only valid entries where both
    'r-product_filter' and 'r_conf_filter' are True.
    """
    filtered_data = [
        entry for entry in data if entry["r-product_filter"] and entry["r_conf_filter"]
    ]
    print(f"Filtered datapoints: {len(filtered_data)}")
    return filtered_data


# Function: Extract numerical features and molecular properties from the JSON data
def extract_features(df):
    """
    Extracts features from 'monomer1_data' and 'monomer2_data' JSON fields
    and adds them as new columns to the input DataFrame.

    Args:
        df (pd.DataFrame): Input DataFrame with 'monomer1_data' and 'monomer2_data' JSON fields.

    Returns:
        pd.DataFrame: DataFrame with additional columns for extracted features.
    """
    # Keys to process with min, max, mean statistics
    keys_to_process = [
        "charges",
        "fukui_electrophilicity",
        "fukui_nucleophilicity",
        "fukui_radical",
    ]

    # Loop through each monomer and process its features
    for monomer_key in ["monomer1_data", "monomer2_data"]:
        # Process specific keys for min, max, mean statistics
        for key in keys_to_process:
            df[f"{monomer_key}_{key}_min"] = df[monomer_key].apply(
                lambda x: min(x[key].values())
                if isinstance(x, dict) and key in x
                else None
            )
            df[f"{monomer_key}_{key}_max"] = df[monomer_key].apply(
                lambda x: max(x[key].values())
                if isinstance(x, dict) and key in x
                else None
            )
            df[f"{monomer_key}_{key}_mean"] = df[monomer_key].apply(
                lambda x: sum(x[key].values()) / len(x[key].values())
                if isinstance(x, dict) and key in x
                else None
            )

        # Process dipole components
        df[f"{monomer_key}_dipole_x"] = df[monomer_key].apply(
            lambda x: x["dipole"][0]
            if isinstance(x, dict) and "dipole" in x and isinstance(x["dipole"], list)
            else None
        )
        df[f"{monomer_key}_dipole_y"] = df[monomer_key].apply(
            lambda x: x["dipole"][1]
            if isinstance(x, dict) and "dipole" in x and isinstance(x["dipole"], list)
            else None
        )
        df[f"{monomer_key}_dipole_z"] = df[monomer_key].apply(
            lambda x: x["dipole"][2]
            if isinstance(x, dict) and "dipole" in x and isinstance(x["dipole"], list)
            else None
        )

    return df


# Function: Create a flipped dataset with reversed monomer order
def create_flipped_dataset(df_features):
    """
    Creates a flipped dataset where the order of monomer1 and monomer2 is swapped.

    Args:
        df_features (pd.DataFrame): The input DataFrame with extracted features.

    Returns:
        pd.DataFrame: A new DataFrame with flipped monomer order.
    """
    flipped_rows = []
    for _, row in df_features.iterrows():
        flipped_row = row.copy()
        flipped_row["monomer1_s"], flipped_row["monomer2_s"] = (
            row["monomer2_s"],
            row["monomer1_s"],
        )
        flipped_rows.append(flipped_row)
    return pd.DataFrame(flipped_rows)


# Load existing embeddings from file if available
def load_embeddings(file_path="embeddings.json"):
    """Load embeddings from a JSON file if it exists."""
    if os.path.exists(file_path):
        with open(file_path, "r") as file:
            embeddings = json.load(file)
            print(f"Loaded {len(embeddings)} embeddings from {file_path}.")
            return {item["name"]: item["embedding"] for item in embeddings}
    return {}


# Save embeddings to a file
def save_embeddings(embeddings_dict, file_path="output/method_embeddings.json"):
    """Save embeddings to a JSON file."""
    embeddings_list = [{"name": name, "embedding": embedding} for name, embedding in embeddings_dict.items()]
    with open(file_path, "w") as file:
        json.dump(embeddings_list, file, indent=4)
    print(f"Saved {len(embeddings_list)} embeddings to {file_path}.")


# Function: Check cache and get embedding
def get_or_create_embedding(client, text, model="text-embedding-3-small"):
    """
    Retrieve embeddings for a given text.
    The function checks the global cache first and only queries the API if the embedding is not already cached.

    Args:
        client: The OpenAI client instance for API requests.
        text (str): The input text to generate an embedding for.
        model (str): The OpenAI model to use for embedding generation (default: "text-embedding-3-small").

    Returns:
        list: The embedding vector as a list of floats.
    """
    global embedding_cache  # Ensure global access to the embedding cache

    # Handle None or NaN text inputs safely
    if text is None or pd.isna(text):
        print("Warning: Found None or NaN in determination_method, returning a zero vector.")
        return [0] * 1536  # Return a zero vector of the expected embedding dimension (e.g., 1536 for this model)

    # Clean the text by removing newlines for consistency
    text_cleaned = text.replace("\n", " ")

    # Check if the embedding already exists in the cache
    if text_cleaned in embedding_cache:
        return embedding_cache[text_cleaned]  # Return the cached embedding

    # If not in the cache, generate the embedding using the OpenAI API
    response = client.embeddings.create(input=[text_cleaned], model=model)
    embedding = response.data[0].embedding  # Extract the embedding from the response

    # Store the new embedding in the cache for future use
    embedding_cache[text_cleaned] = embedding
    return embedding


# Function: Convert str to embeddings and apply PCA
def process_embeddings(df, client, column_name, prefix):
    """
    Processes a specified column into embeddings, applies PCA for dimensionality reduction,
    and adds the reduced components as new features.

    Args:
        df (pd.DataFrame): Input DataFrame containing the column to process.
        client: OpenAI API client for generating embeddings.
        column_name (str): Name of the column to process.
        prefix (str): Prefix for the resulting PCA component columns.

    Returns:
        pd.DataFrame: Updated DataFrame with new PCA-reduced embedding features.
    """
    embeddings = []

    # Generate embeddings or fetch from cache
    for value in df[column_name].unique():
        embedding = get_or_create_embedding(client, value)  # Fetch embedding
        embeddings.append({"name": value, "embedding": embedding})

    # Convert embeddings into DataFrame for PCA
    embedding_matrix = [item["embedding"] for item in embeddings]
    pca = PCA(n_components=2)  # Reduce to 2 components
    reduced_embeddings = pca.fit_transform(embedding_matrix)

    # Map reduced embeddings back to the column values
    embedding_map = {item["name"]: reduced for item, reduced in zip(embeddings, reduced_embeddings)}

    # Add PCA components to the DataFrame
    df[f"{prefix}_1"] = df[column_name].apply(
        lambda x: embedding_map.get(x, [None, None])[0]
    )
    df[f"{prefix}_2"] = df[column_name].apply(
        lambda x: embedding_map.get(x, [None, None])[1]
    )

    print(f"PCA reduced embeddings for {column_name} added as {prefix}_1 and {prefix}_2.")
    return df


def process_logp(df):
    """
    Process LogP values for solvents in the dataframe.
    Returns the dataframe with added LogP column.
    """
    # File to store the cache
    CACHE_FILE = "./output/logp_cache.json"

    if "LogP" not in df.columns:
        print("LogP column not found. Calculating LogP values...")

        # Load existing cache if available
        smiles_cache = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r') as f:
                    smiles_cache = json.load(f)
                print(f"Loaded {len(smiles_cache)} cached values")
            except json.JSONDecodeError:
                print("Cache file corrupted, starting with empty cache")

        def get_cached_logp(solvent):
            # Get SMILES for the solvent
            smiles = utils.name_to_smiles(solvent)
            if not smiles:
                return None

            # If SMILES is already in cache, return cached value
            if smiles in smiles_cache:
                return smiles_cache[smiles]

            # Otherwise calculate LogP and store in cache using SMILES as key
            print(f"Calculating LogP for SMILES: {smiles} (solvent: {solvent})")
            logp = utils.calculate_logP(smiles)
            smiles_cache[smiles] = logp

            # Save updated cache to file
            with open(CACHE_FILE, 'w') as f:
                json.dump(smiles_cache, f, indent=2)

            return logp

        # Process LogP values with progress tracking
        total_solvents = len(df["solvent"].unique())
        print(f"\nProcessing LogP values for {total_solvents} unique solvents...")

        # First create a mapping of all unique solvents to their LogP values
        unique_solvents = df["solvent"].unique()
        solvent_to_logp = {}

        for i, solvent in enumerate(unique_solvents, 1):
            if pd.isna(solvent):
                solvent_to_logp[solvent] = np.nan
                continue

            logp = get_cached_logp(solvent)
            solvent_to_logp[solvent] = logp

            if i % 10 == 0 or i == total_solvents:
                print(f"Processed {i}/{total_solvents} solvents ({(i / total_solvents * 100):.1f}%)")

        # Then map these values to the dataframe
        df["LogP"] = df["solvent"].map(solvent_to_logp)
        print("LogP calculation completed!")

    return df


def main(input_file, output_file):
    # Load JSON data
    with open(input_file, "r") as file:
        data = json.load(file)
    print(f"Initial datapoints: {len(data)}")

    # Filter data
    filtered_data = filter_data(data)

    # Convert to DataFrame
    df = pd.DataFrame(filtered_data)

    # Extract nested fields
    df = extract_nested_fields(df)

    # Extract relevant features and molecular properties
    df = extract_features(df)

    # Load existing embeddings and initialize client
    global embedding_cache
    embedding_cache = load_embeddings()
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Process embeddings for determination_method
    df = process_embeddings(df, client, column_name="method", prefix="method")

    # Process embeddings for polymerization_type
    df = process_embeddings(df, client, column_name="polymerization_type", prefix="polymerization_type")

    # Save updated embeddings to file
    save_embeddings(embedding_cache)

    # Extract r-values
    df["r1"] = df["r_values"].apply(
        lambda x: x["constant_1"] if isinstance(x, dict) and "constant_1" in x else None
    )
    df["r2"] = df["r_values"].apply(
        lambda x: x["constant_2"] if isinstance(x, dict) and "constant_2" in x else None
    )

    # Remove r-values < 0
    df = df[(df["r1"] >= 0) & (df["r2"] >= 0)]

    # Calculate r-product
    df['r_product'] = df['r1'] * df['r2']

    # Set solvent to "bulk" if polymerization type is "bulk"
    df.loc[df["polymerization_type"] == "bulk", "solvent"] = "bulk"
    df["solvent"].replace(['na', 'NA', 'null', 'NULL', None, '', 'nan'], np.nan, inplace=True)

    # Process LogP values
    df = process_logp(df)

    # Define column groups that should not contain NaN values
    mol1_cols = [col for col in df.columns if col.startswith('monomer1_data_')]
    mol2_cols = [col for col in df.columns if col.startswith('monomer2_data_')]
    condition_cols = ['LogP', 'temperature', 'method_1', 'method_2',
                      'polymerization_type_1', 'polymerization_type_2']
    target_cols = ['r1', 'r2', 'r_product']
    names = ['monomer1_s', 'monomer2_s']

    # Combine all columns that should not contain NaN
    columns_to_check = mol1_cols + mol2_cols + condition_cols + target_cols + names

    # Count NaN values before dropping
    nan_counts = df[columns_to_check].isna().sum()
    print("\nNaN counts before filtering:")
    print(nan_counts[nan_counts > 0])  # Only show columns with NaN values

    # Drop rows with NaN in any of the specified columns
    df_cleaned = df.dropna(subset=columns_to_check)

    # Print information about dropped rows
    rows_dropped = len(df) - len(df_cleaned)
    print(f"\nRows dropped due to NaN values: {rows_dropped}")
    print(f"Remaining rows: {len(df_cleaned)}")

    # Save the results
    df_cleaned.to_csv(output_file, index=False)

    print(f"Processed data saved to {output_file}")
    print(f"Final number of datapoints: {len(df_cleaned)}")